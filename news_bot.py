import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher

# 1. 환경 설정 및 클라이언트 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
# 유사도 필터링을 위해 10개를 가져와 5개를 선택
google_news = GNews(language='ko', country='KR', period='2d', max_results=10)

# [V7.0] 에이전트 페르소나 정의
ROLES = {
    "HR": "인사 결정권자. 키워드와 인사이트 에이전트의 성과를 평가하여 채용/해고를 결정함",
    "BA_INTERNAL": "플랫폼 내부 감사관. 플랫폼의 성장을 위해 의사결정의 잘된 점과 잘못된 점을 날카롭게 지적함",
    "PM": "IT 서비스 기획자",
    "BA": "비즈니스 전략 분석가",
    "SEC": "증권 및 투자 시장 분석가"
}

def is_similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def call_agent(prompt, role_key):
    """도메인 에이전트별 특화 분석 수행"""
    persona = ROLES.get(role_key, "전문가")
    full_prompt = f"당신은 {persona}입니다. 다음을 분석하고 인사이트를 주십시오:\n{prompt}"
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=full_prompt)
        return res.text
    except: return "• 분석 데이터 생성 중 오류"

# [V7.0 로직] 모든 사용자 리포트 및 거버넌스 가동
users_res = supabase.table("users").select("*").execute()
users = users_res.data

print(f"📡 총 {len(users)}명의 사용자를 대상으로 팽창 엔진 가동")

for user in users:
    user_id = user['id']
    user_email = user['email']
    
    # 키워드 로드 및 중복 제거 패치
    kw_res = supabase.table("keywords").select("word").eq("user_id", user_id).eq("is_active", True).execute()
    raw_kws = [k['word'] for k in kw_res.data]
    user_keywords = list(dict.fromkeys(raw_kws))[:5] # 중복 제거 후 최대 5개
    
    if not user_keywords: continue

    print(f"🔍 {user_email}님(키워드: {user_keywords}) 분석 및 거버넌스 프로세스 시작")
    
    # 유저별 독립 리포트 객체
    user_report = {
        "date": TODAY, 
        "articles": [], 
        "pm_brief": "", "ba_brief": "", "securities_brief": "",
        "internal_audit": "", "hr_proposal": "", # [V7.0 추가]
        "tracked_keywords": user_keywords
    }
    all_titles = []

    for word in user_keywords:
        news_items = google_news.get_news(word)
        unique_news = []
        for news in news_items:
            if any(is_similar(news['title'], u['title']) > 0.6 for u in unique_news): continue
            unique_news.append(news)
            if len(unique_news) >= 5: break # 키워드당 5개 제한

        for news in unique_news:
            # 3인 인사이트 체제 가동
            article = {
                "keyword": word, "title": news['title'], "url": news['url'],
                "pm_summary": call_agent(news['title'], "PM"),
                "ba_summary": call_agent(news['title'], "BA"),
                "sec_summary": call_agent(news['title'], "SEC")
            }
            user_report["articles"].append(article)
            all_titles.append(f"[{word}] {news['title']}")
            time.sleep(0.5)

    if user_report["articles"]:
        titles_combined = "\n".join(all_titles)
        
        # 1. 3인 체제 종합 브리핑
        user_report["pm_brief"] = call_agent(f"종합 뉴스:\n{titles_combined}", "PM")
        user_report["ba_brief"] = call_agent(f"종합 뉴스:\n{titles_combined}", "BA")
        user_report["securities_brief"] = call_agent(f"종합 뉴스:\n{titles_combined}", "SEC")
        
        # 2. [신규] BA 에이전트 내부 감사 (잘한 점/잘못한 점 지적)
        user_report["internal_audit"] = call_agent(f"오늘의 의사결정(키워드 선택 및 분석 품질)의 잘된 점과 잘못한 점을 지적하세요.", "BA_INTERNAL")
        
        # 3. [신규] HR 에이전트 인사권 행사 (키워드/에이전트 채용 및 해고 제안)
        user_report["hr_proposal"] = call_agent(f"현재 키워드 {user_keywords} 중 성과가 낮은 것을 '해고'하고 신규 키워드 '채용'을 제안하세요.", "HR")

        # 유저별 리포트 DB 저장
        supabase.table("reports").insert({
            "user_id": user_id,
            "report_date": TODAY,
            "content": user_report
        }).execute()
        
        print(f"✅ {user_email}님의 거버넌스 리포트가 성공적으로 저장되었습니다.")
