import os, json, time, traceback, random
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher

# 1. 환경 설정 및 클라이언트 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# [수정] 언어(ko) 및 국가(KR) 제한을 완전히 해제하여 글로벌 뉴스 수집 허용
google_news = GNews(period='2d', max_results=10) 

ROLES = {
    "HR": "인사 결정권자. 키워드 성과 평가 및 채용/해고 제안.",
    "BA_INTERNAL": "플랫폼 내부 감사관. 전략적 결함 비판.",
    "DEBUGGER": "시스템 엔지니어. 코드 오류 분석 및 패치 제안.",
    "PM": "IT 서비스 기획자", "BA": "전략 분석가", "SEC": "증권 분석가"
}

def call_agent(prompt, role_key, max_retries=3):
    """지수 백오프 및 선제적 휴식으로 429 에러 회피"""
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            time.sleep(4.5 + random.uniform(0, 1.5)) # RPM 안정화
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
        except Exception as e:
            if "429" in str(e) or "Quota" in str(e):
                wait_time = (2 ** attempt) * 15 + random.uniform(0, 5)
                print(f"⚠️ {role_key} 지연 발생. {wait_time:.1f}초 후 재시도...")
                time.sleep(wait_time)
            else: raise e
    return f"• 분석 지연 (과부하로 인한 스킵)"

# --- [거버넌스 및 스냅샷 로직] ---

def create_snapshot(approved_by, details):
    """마스터 복구용 스냅샷 (user_settings 구조 반영)"""
    # 현재 모든 유저의 설정 상태를 스냅샷으로 저장
    current_settings = supabase.table("user_settings").select("*").execute().data
    supabase.table("version_snapshots").insert({
        "snapshot_data": {"settings": current_settings},
        "approved_by": approved_by,
        "description": details
    }).execute()

def execute_governance():
    """23:30 자동 승인 집행 로직"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    proposals = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute().data
    
    for p in proposals:
        if now >= deadline:
            print(f"🤖 [Auto-Gov] {p['word']} 집행 중...")
            # [수정] user_settings의 keywords 배열을 직접 수정하는 로직 필요 (필요 시 구현)
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], "action_type": p['type'], "target_word": p['word'],
                "execution_method": "AUTO_SYSTEM", "details": "23:30 타임아웃 자동 집행"
            }).execute()

# --- [메인 엔진: user_settings 테이블 대응] ---

def run_main_engine():
    # [수정] user_settings 테이블에서 유저별 키워드 배열을 직접 가져옴
    settings = supabase.table("user_settings").select("*").execute().data
    
    for user_set in settings:
        user_id = user_set['id']
        user_email = user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5] # 최대 5개
        
        if not user_keywords:
            print(f"⏩ {user_email}님 키워드 없음. 스킵.")
            continue

        print(f"🔍 {user_email}님 글로벌 분석 시작: {user_keywords}")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            # 키워드별 뉴스 수집 (언어 제한 없음)
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
                all_titles.append(f"[{word}] {n['title']}")

        if report["articles"]:
            context = "\n".join(all_titles)
            report["pm_brief"] = call_agent(context, "PM")
            report["ba_brief"] = call_agent(context, "BA")
            report["securities_brief"] = call_agent(context, "SEC")
            report["internal_audit"] = call_agent("의사결정 및 분석 품질 비판", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 성과 평가 및 해고/채용 제안", "HR")
            
            # 최종 리포트 저장
            supabase.table("reports").insert({"user_id": user_id, "report_date": TODAY, "content": report}).execute()
            print(f"✅ {user_email}님 글로벌 리포트 저장 완료.")

if __name__ == "__main__":
    try:
        execute_governance()
        run_main_engine()
    except Exception as e:
        print(f"🚨 시스템 오류: {str(e)}")
        # 필요 시 handle_exception(traceback.format_exc()) 호출
