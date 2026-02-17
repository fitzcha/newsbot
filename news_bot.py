import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime

# 1. 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

MASTER_EMAIL = "positivecha@gmail.com"
TODAY = datetime.now().strftime("%Y-%m-%d")

# 2. 클라이언트 초기화
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='2d', max_results=2)

def analyze_news(title, role="PM"):
    prompt = f"당신은 {role}입니다. 뉴스 '{title}'을 3개 불릿 포인트로 요약하고 인사이트를 주십시오. 단문으로 작성하세요."
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except: return "• 분석 중 오류 발생"

def send_newsletter(user_email, user_articles):
    if not user_articles: return
    user_id = user_email.split('@')[0]
    email_body = f"<h2>🚀 {user_id}님, 오늘 설정하신 키워드 분석 리포트입니다.</h2>"
    for a in user_articles:
        email_body += f"<div style='margin-bottom:20px; border-bottom:1px solid #eee; padding-bottom:10px;'><p><span style='background:#eef2f7; padding:4px 8px; border-radius:4px;'>#{a['keyword']}</span></p><h3 style='color:#007bff; margin-top:5px;'>{a['title']}</h3><div style='background:#f9f9f9; padding:15px; border-radius:8px;'>{a['pm_summary'].replace('•', '<br>•')}</div><a href='{a['url']}' style='font-size:0.8em; color:#007bff;'>원문 보기 ↗</a></div>"
    try:
        resend.Emails.send({"from": "Fitz Intelligence <onboarding@resend.dev>", "to": user_email, "subject": f"[{TODAY}] {user_id}님을 위한 인사이트", "html": email_body})
        print(f"📧 {user_email}님 이메일 발송 성공")
    except Exception as e: print(f"❌ 이메일 발송 실패: {e}")

# 3. 메인 실행 로직
response = supabase.table("user_settings").select("*").execute()
users = response.data
master_report = {"date": TODAY, "articles": [], "pm_brief": "", "ba_brief": "", "tracked_keywords": []}

for user in users:
    user_email = user.get('email')
    user_keywords = user.get('keywords', [])
    if not user_email: continue
    
    user_articles = []
    print(f"🔍 {user_email}님의 키워드 수집 중...")
    
    for word in user_keywords:
        news_items = google_news.get_news(word)
        for news in news_items:
            pm_sum = analyze_news(news['title'], "PM")
            ba_sum = analyze_news(news['title'], "BA") # BA 요약 추가
            article_data = {"keyword": word, "title": news['title'], "url": news['url'], "pm_summary": pm_sum, "ba_summary": ba_sum}
            user_articles.append(article_data)
            
            if user_email == MASTER_EMAIL:
                master_report["articles"].append(article_data)
                if word not in master_report["tracked_keywords"]:
                    master_report["tracked_keywords"].append(word)

    send_newsletter(user_email, user_articles)

# [V5.0 핵심] DB 저장 로직 추가
if master_report["articles"]:
    titles = [a['title'] for a in master_report["articles"]]
    master_report["pm_brief"] = analyze_news(f"전체 요약:\n{chr(10).join(titles)}", "PM")
    master_report["ba_brief"] = analyze_news(f"비즈니스 분석:\n{chr(10).join(titles)}", "BA")
    
    master_user = next((u for u in users if u['email'] == MASTER_EMAIL), None)
    if master_user:
        supabase.table("reports").insert({
            "user_id": master_user['id'],
            "report_date": TODAY,
            "content": master_report
        }).execute()
        print(f"🚀 {TODAY} 리포트 DB 저장 완료!")
