import os, json, time, traceback, random, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# [v9.3] 타임존 설정: 한국(KST) 시간 강제 적용
# 서버가 UTC여도 무조건 한국 날짜 기준으로 DB에 저장합니다.
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")

# 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

ROLES = {
    "HR": "인사 결정권자. 키워드 성과 평가 및 채용/해고 제안.",
    "BA_INTERNAL": "플랫폼 내부 감사관. 전략적 결함 및 품질 비판.",
    "DEBUGGER": "시스템 엔지니어. 코드 안정성 분석.",
    "PM": "IT 서비스 기획자", 
    "BA": "전략 분석가", 
    "SEC": "증권 분석가"
}

# [v9.3] 이메일 발송 최적화 (Resend SDK 규격 준수)
def send_email_report(user_email, report_data):
    try:
        articles_html = "".join([
            f"<li style='margin-bottom:15px;'><b>[{a['keyword']}] {a['title']}</b><br>"
            f"<span style='color:#666; font-size:0.9em;'>{a['pm_summary'][:150]}...</span> "
            f"<a href='{a['url']}' style='color:#007bff; text-decoration:none;'>원문보기</a></li>"
            for a in report_data['articles']
        ])
        
        # [주의] Resend 무료 티어는 승인된 도메인이 없을 경우 본인 이메일로만 발송 가능할 수 있음
        params = {
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": [user_email], # 리스트 형태로 전달
            "subject": f"[{TODAY}] Fitz Intelligence 분석 리포트",
            "html": f"""
            <div style="font-family:sans-serif; max-width:600px; margin:auto; border:1px solid #eee; padding:20px;">
                <h2 style="color:#007bff;">🚀 {TODAY} 인사이트 리포트</h2>
                <p>{user_email}님, 오늘의 뉴스 분석 결과입니다.</p>
                <div style="background:#f8f9fa; padding:15px; border-radius:10px;">{report_data['pm_brief']}</div>
                <h3 style="margin-top:20px;">📰 주요 뉴스</h3>
                <ul>{articles_html}</ul>
            </div>
            """
        }
        
        resend.Emails.send(params)
        print(f"📧 {user_email}님 이메일 발송 명령 완료 (KST {TODAY})")
    except Exception as e:
        print(f"🚨 이메일 발송 실패: {str(e)}")

# AI 에이전트 호출 (백오프 로직 유지)
def call_agent(prompt, role_key, max_retries=3):
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            time.sleep(5 + random.uniform(0, 2)) 
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
        except Exception as e:
            if "429" in str(e):
                wait = (2 ** attempt) * 30
                print(f"⚠️ 과부하 대기: {wait}초 ({role_key})")
                time.sleep(wait)
            else: raise e
    return "• 분석 지연"

# 거버넌스 집행 (23:30)
def execute_governance():
    now_kst = datetime.now(KST)
    # KST 기준 밤 11:30분 확인
    deadline = now_kst.replace(hour=23, minute=30, second=0, microsecond=0)
    
    res = supabase.table("pending_approvals").select("*").neq("status", "EXECUTED").execute()
    for p in (res.data or []):
        if now_kst >= deadline or p['status'] in ['APPROVED', 'REJECTED']:
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], "action_type": p['type'], 
                "target_word": p['word'], "execution_method": "AUTO_SYSTEM",
                "details": f"KST {deadline} 기준 자동 확정"
            }).execute()

# 메인 엔진
def run_main_engine():
    settings = supabase.table("user_settings").select("*").execute().data or []
    
    for user_set in settings:
        user_id, user_email = user_set['id'], user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5]
        
        if not user_keywords: continue

        print(f"🔍 {user_email} 분석 시작 (기준일: {TODAY})")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            
            # GNews 인스턴스
            gn = GNews(language=lang, country=country, period='1d', max_results=5)
            items = gn.get_news(word)

            if not items:
                gn = GNews(language=lang, country=country, period='3d', max_results=5)
                items = gn.get_news(word)

            unique_news = []
            for n in items:
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
            ctx = "\n".join(all_titles)
            report["pm_brief"] = call_agent(ctx, "PM")
            report["ba_brief"] = call_agent(ctx, "BA")
            report["securities_brief"] = call_agent(ctx, "SEC")
            report["internal_audit"] = call_agent("플랫폼 분석 품질 비판", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 기반 제안", "HR")
            
            # [v9.3 핵심] 한국 날짜(KST)로 DB 저장
            supabase.table("reports").upsert({
                "user_id": user_id, 
                "report_date": TODAY, 
                "content": report
            }).execute()
            
            send_email_report(user_email, report)
            print(f"✅ {user_email} 완료.")

if __name__ == "__main__":
    execute_governance()
    run_main_engine()
