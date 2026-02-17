import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher

# 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
# 유사도 필터링을 위해 10개를 가져와 5개를 선택
google_news = GNews(language='ko', country='KR', period='2d', max_results=10)

def is_similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def analyze_news(title, role="PM"):
    prompt = f"당신은 {role}입니다. 뉴스 '{title}'을 3개 불릿 포인트로 요약하고 인사이트를 주십시오."
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except: return "• 분석 중 오류 발생"

# [V5.6 로직] 모든 사용자 리포트 개별 생성
users_res = supabase.table("users").select("*").execute()
users = users_res.data

print(f"📡 총 {len(users)}명의 사용자를 분석합니다.")

for user in users:
    user_id = user['id']
    user_email = user['email']
    
    kw_res = supabase.table("keywords").select("word").eq("user_id", user_id).eq("is_active", True).execute()
    user_keywords = [k['word'] for k in kw_res.data][:5] # 키워드 5개 제한
    
    if not user_keywords: continue

    print(f"🔍 {user_email}님(키워드: {user_keywords}) 분석 중...")
    
    # [핵심] 유저별 독립 리포트 객체 생성
    user_report = {"date": TODAY, "articles": [], "pm_brief": "", "ba_brief": "", "securities_brief": "", "tracked_keywords": user_keywords}
    all_titles = []

    for word in user_keywords:
        news_items = google_news.get_news(word)
        unique_news = []
        for news in news_items:
            if any(is_similar(news['title'], u['title']) > 0.6 for u in unique_news): continue
            unique_news.append(news)
            if len(unique_news) >= 5: break # 키워드당 5개 제한

        for news in unique_news:
            pm_sum = analyze_news(news['title'], "PM")
            ba_sum = analyze_news(news['title'], "BA")
            # 증권 에이전트 추가
            sec_sum = analyze_news(news['title'], "증권 분석가")
            
            article = {"keyword": word, "title": news['title'], "url": news['url'], "pm_summary": pm_sum, "ba_summary": ba_sum, "sec_summary": sec_sum}
            user_report["articles"].append(article)
            all_titles.append(f"[{word}] {news['title']}")
            time.sleep(1)

    # [핵심] 유저별 리포트 DB 저장 (마스터 제한 해제)
    if user_report["articles"]:
        titles_combined = "\n".join(all_titles)
        user_report["pm_brief"] = analyze_news(f"종합 요약:\n{titles_combined}", "PM")
        user_report["ba_brief"] = analyze_news(f"비즈니스 분석:\n{titles_combined}", "BA")
        user_report["securities_brief"] = analyze_news(f"증권 시장 분석:\n{titles_combined}", "증권 분석가")
        
        supabase.table("reports").insert({
            "user_id": user_id,
            "report_date": TODAY,
            "content": user_report
        }).execute()
        print(f"✅ {user_email}님의 리포트가 성공적으로 저장되었습니다.")
