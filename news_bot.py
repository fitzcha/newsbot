import os, json, gspread, time
from google import genai
from gnews import GNews
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# 1. 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_JSON = os.environ.get("GOOGLE_SHEETS_JSON")
TODAY = datetime.now().strftime("%Y-%m-%d")

# 2. 클라이언트 초기화
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GOOGLE_JSON), scope)
client = gspread.authorize(creds)
sheet = client.open("Mobility_Policy_Manager").sheet1 
google_genai = genai.Client(api_key=GEMINI_KEY)

# 3. 설정 및 구조화 분석 함수
keywords = [k for k in sheet.col_values(1) if k.strip()][:5]
google_news = GNews(language='ko', country='KR', period='2d', max_results=2)

def analyze_by_role(word, title, role="PM"):
    role_desc = "모빌리티 서비스 기획자(PM)" if role == "PM" else "비즈니스 분석가(BA)"
    prompt = f"""
    당신은 {role_desc}입니다. 다음 뉴스 제목을 분석하여 3~5개의 불릿 포인트(•)로 요약하세요.
    제목: {title}
    인사이트 중심의 짧은 문장을 사용할 것. 마크다운 형식을 지킬 것.
    """
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except: return "• 분석 데이터를 생성할 수 없습니다."

# 4. 데이터 수집 (v3.3 로직 포함)
daily_report = {
    "date": TODAY, 
    "tracked_keywords": keywords, # 👈 v3.3 핵심: 전체 키워드 저장
    "articles": [], 
    "pm_brief": "", 
    "ba_brief": ""
}
news_context = ""

for word in keywords:
    print(f"'{word}' 분석 중...")
    articles = google_news.get_news(word)
    for news in articles:
        try:
            time.sleep(1)
            pm_sum = analyze_by_role(word, news['title'], "PM")
            ba_sum = analyze_by_role(word, news['title'], "BA")
            daily_report["articles"].append({
                "keyword": word, "title": news['title'], "url": news['url'],
                "pm_summary": pm_sum, "ba_summary": ba_sum
            })
            news_context += f"[{word}] {news['title']}\n"
        except: continue

# 5. 종합 브리핑 및 저장
if news_context:
    for role in ["PM", "BA"]:
        prompt = f"다음 뉴스 목록을 보고 {role}에게 중요한 전략 3가지를 불릿 포인트로 제안해줘:\n{news_context}"
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        daily_report[f"{role.lower()}_brief"] = res.text

file_path = "data.json"
try:
    with open(file_path, "r", encoding="utf-8") as f: full_data = json.load(f)
except: full_data = []

full_data = [d for d in full_data if d['date'] != TODAY]
full_data.insert(0, daily_report)
with open(file_path, "w", encoding="utf-8") as f: json.dump(full_data, f, ensure_ascii=False, indent=2)
print(f"✅ {TODAY} 엔진 업데이트 완료")
