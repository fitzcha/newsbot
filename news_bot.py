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

ROLES = {
    "HR": "인사 결정권자. 성과 평가 기반 채용/해고 제안.",
    "BA_INTERNAL": "플랫폼 감사관. 의사결정 품질 비판.",
    "PM": "IT 서비스 기획자", "BA": "전략 분석가", "SEC": "증권 분석가"
}

def call_agent(prompt, role_key, max_retries=3):
    """429 에러 방지를 위한 적응형 호출 로직"""
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            # API 가이드를 준수하는 선제적 휴식
            time.sleep(5 + random.uniform(0, 2)) 
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
        except Exception as e:
            if "429" in str(e):
                time.sleep((2 ** attempt) * 20)
            else: raise e
    return "• 분석 지연"

def execute_governance():
    """23:30 의사결정 확정 및 타임락 집행"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    
    # 미확정된 모든 결정(APPROVED, REJECTED, PENDING) 조회
    decisions = supabase.table("pending_approvals").select("*").neq("status", "EXECUTED").execute().data
    
    for p in decisions:
        # 타임아웃이 되었거나 마스터가 이미 결정을 내린 경우 확정 처리
        if now >= deadline or p['status'] in ['APPROVED', 'REJECTED']:
            print(f"🔒 결정 확정: {p['word']} ({p['status']})")
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], "action_type": p['type'], "target_word": p['word'],
                "execution_method": "AUTO_FINALIZER", "details": "23:30 타임락 집행 및 확정"
            }).execute()

def run_main_engine():
    # user_settings 테이블에서 유저별 키워드 배열 로드
    settings = supabase.table("user_settings").select("*").execute().data
    
    for user_set in settings:
        user_id = user_set['id']
        user_email = user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5]
        
        if not user_keywords: continue
        print(f"🔍 {user_email}님 분석 시작: {user_keywords}")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            # [V8.6 핵심] CJK(한중일) 키워드 판별 검색 전략
            is_cjk = any(ord(char) > 0x1100 for char in word)
            
            # 1차 시도: 해당 언어권 정밀 검색
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            gn = GNews(language=lang, country=country, period='2d', max_results=10)
            items = gn.get_news(word)

            # 2차 시도: 결과 없으면 글로벌 확장 검색
            if not items:
                items = GNews(period='2d', max_results=10).get_news(word)

            unique_news = []
            for n in items:
                # [유지] 성환님의 0.6 유사도 필터링 원칙
                if any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_news):
                    continue
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
            report["internal_audit"] = call_agent("품질 감사", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 성과 기반 제안", "HR")
            
            supabase.table("reports").insert({"user_id": user_id, "report_date": TODAY, "content": report}).execute()
            print(f"✅ {user_email}님 리포트 저장 성공.")

if __name__ == "__main__":
    try:
        execute_governance()
        run_main_engine()
    except Exception as e:
        print(f"🚨 치명적 에러: {traceback.format_exc()}")
