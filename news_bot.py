import os, json, time, traceback, random, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher

# 1. 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")
TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

ROLES = {
    "HR": "인사 결정권자. 성과 평가 기반 제안.",
    "BA_INTERNAL": "플랫폼 감사관. 의사결정 비판.",
    "PM": "IT 서비스 기획자", "BA": "전략 분석가", "SEC": "증권 분석가"
}

def send_email_report(user_email, report_data):
    """분석 성공 시에만 이메일 발송"""
    try:
        articles_html = "".join([f"<li><b>[{a['keyword']}] {a['title']}</b><br><a href='{a['url']}'>원문보기</a></li><br>" for a in report_data['articles']])
        html_content = f"<h2>🚀 {TODAY} Fitz Intelligence</h2><p>{user_email}님, 분석 결과입니다.</p><hr><h3>📊 브리핑</h3><div>{report_data['pm_brief']}</div><h3>📰 뉴스</h3><ul>{articles_html}</ul>"
        resend.Emails.send({"from": "Fitz Intelligence <onboarding@resend.dev>", "to": user_email, "subject": f"[{TODAY}] 데일리 뉴스 리포트", "html": html_content})
    except: print("🚨 메일 발송 오류")

def call_agent(prompt, role_key, max_retries=3):
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            time.sleep(5 + random.uniform(0, 2)) 
            res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=f"당신은 {persona}입니다.\n{prompt}")
            return res.text
        except Exception as e:
            if "429" in str(e): time.sleep((2 ** attempt) * 20)
            else: raise e
    return "• 분석 지연"

def execute_governance():
    """23:30 결정 확정 로직"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    res = supabase.table("pending_approvals").select("*").neq("status", "EXECUTED").execute()
    for p in (res.data if res.data else []):
        if now >= deadline or p['status'] in ['APPROVED', 'REJECTED']:
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()

def run_main_engine():
    settings = supabase.table("user_settings").select("*").execute().data
    for user_set in (settings if settings else []):
        user_id, user_email = user_set['id'], user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5]
        if not user_keywords: continue

        print(f"🔍 {user_email}님 분석 중...")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            gn = GNews(language=lang, country=country, period='1d', max_results=10)
            items = gn.get_news(word)
            if not items:
                items = GNews(language=lang, country=country, period='3d', max_results=10).get_news(word)

            unique_news = []
            for n in items:
                if any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_news): continue
                unique_news.append(n)
                if len(unique_news) >= 3: break

            for n in unique_news:
                article = {"keyword": word, "title": n['title'], "url": n['url'], "pm_summary": call_agent(n['title'], "PM"), "ba_summary": call_agent(n['title'], "BA"), "sec_summary": call_agent(n['title'], "SEC")}
                report["articles"].append(article)
                all_titles.append(f"[{word}] {n['title']}")

        if report["articles"]:
            context = "\n".join(all_titles)
            report["pm_brief"] = call_agent(context, "PM")
            report["ba_brief"] = call_agent(context, "BA")
            report["securities_brief"] = call_agent(context, "SEC")
            report["internal_audit"] = call_agent("품질 감사", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 제안", "HR")
            supabase.table("reports").insert({"user_id": user_id, "report_date": TODAY, "content": report}).execute()
            send_email_report(user_email, report)
        else:
            print(f"⚠️ {user_email}님 검색 결과 없음.")

if __name__ == "__main__":
    try:
        execute_governance()
        run_main_engine()
    except Exception as e:
        print(f"🚨 오류: {traceback.format_exc()}")
