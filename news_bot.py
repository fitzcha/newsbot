import os, json, time
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime

# 1. 환경 설정 (GitHub Secrets에 SUPABASE_URL, SUPABASE_KEY 추가 필요)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
MASTER_EMAIL = "positivecha@gmail.com"
TODAY = datetime.now().strftime("%Y-%m-%d")

# 2. 클라이언트 초기화
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='2d', max_results=2)

def analyze_news(title, role="PM"):
    """v3.5에서 정립한 불릿 포인트 기반 스캐닝 최적화 분석"""
    role_desc = "모빌리티 PM" if role == "PM" else "비즈니스 분석가"
    prompt = f"당신은 {role_desc}입니다. 뉴스 '{title}'을 3~5개 불릿 포인트로 요약하고 인사이트를 주십시오. 단문으로 작성하세요."
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except: return "• 분석 중 오류 발생"

# 3. 모든 유저 설정 로드
def get_all_users():
    # user_settings 테이블에서 모든 유저의 ID, 이메일, 키워드를 가져옴
    response = supabase.table("user_settings").select("*").execute()
    return response.data

# 4. 실행 메인 로직
users = get_all_users()
master_report = {"date": TODAY, "articles": [], "pm_brief": "", "ba_brief": "", "tracked_keywords": []}

print(f"🚀 총 {len(users)}명의 유저 분석을 시작합니다.")

for user in users:
    user_email = user.get('email', 'Unknown')
    user_keywords = user.get('keywords', [])
    print(f"--- [{user_email}]님의 키워드 {len(user_keywords)}개 분석 중 ---")
    
    user_articles = []
    
    for word in user_keywords:
        news_items = google_news.get_news(word)
        for news in news_items:
            pm_sum = analyze_news(news['title'], "PM")
            ba_sum = analyze_news(news['title'], "BA")
            
            article_data = {
                "keyword": word,
                "title": news['title'],
                "url": news['url'],
                "pm_summary": pm_sum,
                "ba_summary": ba_sum
            }
            user_articles.append(article_data)
            
            # 마스터(성환님) 데이터는 공용 대시보드(data.json)를 위해 별도 저장
            if user_email == MASTER_EMAIL:
                master_report["articles"].append(article_data)
                if word not in master_report["tracked_keywords"]:
                    master_report["tracked_keywords"].append(word)

    # TODO: 여기서 개별 뉴스레터 발송 함수(send_email)를 호출할 예정입니다.
    print(f"✅ {user_email}님 분석 완료 (기사 {len(user_articles)}건)")

# 5. 마스터 리포트(공개용) 저장
if master_report["articles"]:
    # 종합 브리핑 생성 생략(기존 로직 동일) 후 data.json 저장
    file_path = "data.json"
    try:
        with open(file_path, "r", encoding="utf-8") as f: full_data = json.load(f)
    except: full_data = []
    
    full_data = [d for d in full_data if d['date'] != TODAY]
    full_data.insert(0, master_report)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(full_data, f, ensure_ascii=False, indent=2)

print("🏁 모든 유저 분석 및 마스터 리포트 갱신 완료!")
