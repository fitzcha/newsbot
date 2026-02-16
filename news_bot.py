import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime

# 1. 환경 설정 (GitHub Secrets에 RESEND_API_KEY 추가 필수!)
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

# [핵심] 이메일 ID 추출 및 뉴스레터 발송 함수
def send_newsletter(user_email, user_articles):
    if not user_articles: return
    
    # 이메일 주소에서 @ 앞부분만 추출 (예: positivecha@gmail.com -> positivecha)
    user_id = user_email.split('@')[0]
    
    email_body = f"<h2>🚀 {user_id}님, 오늘 설정하신 키워드 분석 리포트입니다.</h2>"
    for a in user_articles:
        email_body += f"""
        <div style='margin-bottom:20px; border-bottom:1px solid #eee; padding-bottom:10px;'>
            <p><span style='background:#eef2f7; padding:4px 8px; border-radius:4px;'>#{a['keyword']}</span></p>
            <h3 style='color:#007bff; margin-top:5px;'>{a['title']}</h3>
            <div style='background:#f9f9f9; padding:15px; border-radius:8px;'>{a['pm_summary'].replace('•', '<br>•')}</div>
            <a href='{a['url']}' style='font-size:0.8em; color:#007bff;'>원문 보기 ↗</a>
        </div>
        """
    email_body += f"<p style='color:#999; font-size:0.8em;'>본 리포트는 {TODAY} Fitz Intelligence AI에 의해 자동 생성되었습니다.</p>"

    try:
        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": user_email,
            "subject": f"[{TODAY}] {user_id}님만을 위한 모빌리티 인사이트가 도착했습니다.",
            "html": email_body
        })
        print(f"📧 {user_email}님에게 맞춤 리포트 발송 성공!")
    except Exception as e:
        print(f"❌ 발송 실패 ({user_email}): {e}")

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
            article_data = {"keyword": word, "title": news['title'], "url": news['url'], "pm_summary": pm_sum}
            user_articles.append(article_data)
            
            if user_email == MASTER_EMAIL:
                master_report["articles"].append(article_data)
                if word not in master_report["tracked_keywords"]:
                    master_report["tracked_keywords"].append(word)

    send_newsletter(user_email, user_articles)

# [마스터 리포트 data.json 저장 로직 생략 - v4.0과 동일]
