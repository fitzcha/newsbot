import os, json, time, traceback, random, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, time as dt_time
from difflib import SequenceMatcher

# 1. 환경 설정 및 클라이언트 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
GITHUB_TOKEN = os.environ.get("GH_TOKEN") # 셀프 힐링 배포용
TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='2d', max_results=10)

# [V8.0] 에이전트 페르소나 강화 (유지)
ROLES = {
    "HR": "인사 결정권자. 키워드 성과를 평가해 채용/해고를 결정함.",
    "BA_INTERNAL": "플랫폼 내부 감사관. 의사결정의 전략적 결함을 비판함.",
    "DEBUGGER": "시스템 엔지니어. 코드 오류를 분석하고 최적의 패치를 제안함.",
    "PM": "IT 서비스 기획자", "BA": "전략 분석가", "SEC": "증권 분석가"
}

# [V8.3 핵심] 기존 call_agent 로직을 안정화 버전으로 업그레이드
def call_agent(prompt, role_key, max_retries=3):
    """지수 백오프 및 선제적 휴식 로직 적용"""
    persona = ROLES.get(role_key, "전문가")
    
    for attempt in range(max_retries):
        try:
            # [안전장치 1] API 호출 전 무조건 4~5초간 선제적 휴식 (무료 RPM 제한 준수)
            time.sleep(4.5 + random.uniform(0, 1.5)) 
            
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
            
        except Exception as e:
            # [안전장치 2] 429(Rate Limit) 감지 시 재시도 간격 대폭 확대
            if "429" in str(e) or "Quota" in str(e):
                wait_time = (2 ** attempt) * 15 + random.uniform(0, 5)
                print(f"⚠️ {role_key} 에이전트 요청 제한 발생. {wait_time:.1f}초 후 재시도 ({attempt+1}/{max_retries})")
                time.sleep(wait_time)
            else:
                # 기타 에러는 상위 handle_exception으로 전달
                raise e
                
    return f"• 분석 지연 (사유: 과도한 요청으로 인한 분석 스킵)"

# --- [Step 2 신규 로직: 거버넌스 집행] (원문 유지) ---

def create_snapshot(approved_by, details):
    """마스터 복구용 스냅샷 생성 (롤백의 기준점)"""
    active_kws = supabase.table("keywords").select("word").eq("is_active", True).execute().data
    snapshot = {
        "keywords": [k['word'] for k in active_kws],
        "timestamp": datetime.now().isoformat()
    }
    supabase.table("version_snapshots").insert({
        "snapshot_data": snapshot,
        "approved_by": approved_by,
        "description": details
    }).execute()

def execute_governance():
    """23:30 이후 자동 승인 및 마스터 승인 건 집행"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    proposals = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute().data
    
    for p in proposals:
        is_timeout = now >= deadline
        if is_timeout:
            method = "AUTO_SYSTEM"
            print(f"⏰ {p['word']} 자동 승인 타임아웃 집행 시작")
            if p['type'] == 'FIRE':
                supabase.table("keywords").update({"is_active": False}).eq("user_id", p['user_id']).eq("word", p['word']).execute()
            elif p['type'] == 'HIRE':
                supabase.table("keywords").insert({"user_id": p['user_id'], "word": p['word'], "is_active": True}).execute()
            
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], "action_type": p['type'], "target_word": p['word'],
                "execution_method": method, "details": "23:30 시스템 자동 집행 완료"
            }).execute()
            create_snapshot("AUTO_SYSTEM", f"자동 집행: {p['word']}")

# --- [Step 2 신규 로직: 셀프 힐링] (원문 유지) ---

def handle_exception(error_msg):
    """에러 발생 시 디버거 가동 및 마스터 보고"""
    print(f"🚨 시스템 오류 감지: {error_msg}")
    try:
        debug_insight = call_agent(f"다음 에러 로그를 분석하고 해결 코드를 제안하세요: {error_msg}", "DEBUGGER")
        supabase.table("action_logs").insert({
            "action_type": "ERROR_FIX", "execution_method": "AI_DEBUGGER",
            "details": f"에러 분석: {debug_insight}", "target_word": "SYSTEM_CORE"
        }).execute()
    except:
        print("최종 예외 처리 중 에러 발생")

# --- [메인 뉴스 분석 로직] (원문 유지) ---

def run_main_engine():
    users = supabase.table("users").select("*").execute().data
    for user in users:
        user_id, user_email = user['id'], user['email']
        kw_res = supabase.table("keywords").select("word").eq("user_id", user_id).eq("is_active", True).execute()
        user_keywords = list(dict.fromkeys([k['word'] for k in kw_res.data]))[:5]
        
        if not user_keywords: continue
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            items = google_news.get_news(word)
            unique_news = []
            for n in items:
                if any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_news): continue
                unique_news.append(n)
                if len(unique_news) >= 3: break

            for n in unique_news:
                article = {
                    "keyword": word, "title": n['title'], "url": n['url'],
                    "pm_summary": call_agent(n['title'], "PM"),
                    "ba_summary": call_agent(n['title'], "BA"),
                    "sec_summary": call_agent(n['title'], "SEC")
                }
                report["articles"].append(article)
                all_titles.append(n['title'])
                # time.sleep(1) # call_agent 내부의 선제적 휴식으로 대체됨

        if report["articles"]:
            context = "\n".join(all_titles)
            report["pm_brief"] = call_agent(context, "PM")
            report["ba_brief"] = call_agent(context, "BA")
            report["securities_brief"] = call_agent(context, "SEC")
            report["internal_audit"] = call_agent("오늘의 분석 품질을 비판하세요.", "BA_INTERNAL")
            hr_proposal_text = call_agent(f"키워드 {user_keywords} 중 하나를 추천 해고하고 신규를 제안하세요.", "HR")
            report["hr_proposal"] = hr_proposal_text
            supabase.table("reports").insert({"user_id": user_id, "report_date": TODAY, "content": report}).execute()

if __name__ == "__main__":
    try:
        execute_governance()
        run_main_engine()
    except Exception as e:
        handle_exception(traceback.format_exc())
