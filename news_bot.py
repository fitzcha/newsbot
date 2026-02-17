import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime

# 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

MASTER_EMAIL = "positivecha@gmail.com"
TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='2d', max_results=2)

def analyze_news(title, role="PM"):
    prompt = f"당신은 {role}입니다. 뉴스 '{title}'을 3개 불릿 포인트로 요약하고 인사이트를 주십시오."
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except: return "• 분석 중 오류 발생"

# [V5.0 로직] 모든 사용자 가져오기
users_res = supabase.table("users").select("*").execute()
users = users_res.data
master_report = {"date": TODAY, "articles": [], "pm_brief": "", "ba_brief": "", "tracked_keywords": []}

print(f"📡 총 {len(users)}명의 사용자를 발견했습니다.")

for user in users:
    user_id = user['id']
    user_email = user['email']
    
    # 해당 사용자의 키워드만 가져오기
    kw_res = supabase.table("keywords").select("word").eq("user_id", user_id).eq("is_active", True).execute()
    user_keywords = [k['word'] for k in kw_res.data]
    
    if not user_keywords:
        print(f"⏩ {user_email}님은 설정된 키워드가 없어 건너뜁니다.")
        continue

    print(f"🔍 {user_email}님의 키워드({user_keywords}) 분석 시작...")
    user_articles = []

    for word in user_keywords:
        news_items = google_news.get_news(word)
        for news in news_items:
            pm_sum = analyze_news(news['title'], "PM")
            ba_sum = analyze_news(news['title'], "BA")
            article = {"keyword": word, "title": news['title'], "url": news['url'], "pm_summary": pm_sum, "ba_summary": ba_sum}
            user_articles.append(article)
            if user_email == MASTER_EMAIL:
                master_report["articles"].append(article)
                if word not in master_report["tracked_keywords"]: master_report["tracked_keywords"].append(word)

    # 리포트 DB 저장 (마스터 전용)
    if user_email == MASTER_EMAIL and user_articles:
        titles = [a['title'] for a in user_articles]
        master_report["pm_brief"] = analyze_news(f"종합 요약:\n{chr(10).join(titles)}", "PM")
        master_report["ba_brief"] = analyze_news(f"비즈니스 분석:\n{chr(10).join(titles)}", "BA")
        
        supabase.table("reports").insert({
            "user_id": user_id,
            "report_date": TODAY,
            "content": master_report
        }).execute()
        print(f"🚀 {user_email}님의 리포트가 DB에 저장되었습니다!")
