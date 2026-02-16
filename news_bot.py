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

# 3. 설정 및 분석 함수
keywords = [k for k in sheet.col_values(1) if k.strip()][:5]
google_news = GNews(language='ko', country='KR', period='1d', max_results=2)

def analyze_by_role(word, title, role="PM"):
    prompts = {
        "PM": f"모빌리티 서비스 기획자로서 이 뉴스의 시장 동향과 시사점을 1문장 요약해줘: {title}",
        "BA": f"비즈니스 분석가로서 이 뉴스가 해당 기업의 수익 구조나 사업 확장에 미칠 영향을 1문장 요약해줘: {title}"
    }
    try:
        res = google_genai.models.generate_content(
            model="gemini-2.0-flash", 
            contents=prompts.get(role, prompts["PM"])
        )
        return res.text
    except: return "분석 데이터 생성 중..."

# 4. 데이터 수집 및 실행
daily_report = {"date": TODAY, "articles": [], "pm_brief": "", "ba_brief": ""}
news_context = ""

for word in keywords:
    print(f"'{word}' 분석 중...")
    for news in google_news.get_news(word):
        try:
            time.sleep(1) # 유료 플랜이므로 짧게 휴식
            pm_sum = analyze_by_role(word, news['title'], "PM")
            ba_sum = analyze_by_role(word, news['title'], "BA")
            
            daily_report["articles"].append({
                "keyword": word,
                "title": news['title'],
                "url": news['url'], # 원문 링크 저장
                "pm_summary": pm_sum,
                "ba_summary": ba_sum
            })
            news_context += f"[{word}] {news['title']}\n"
        except: continue

# 5. 직군별 종합 브리핑 생성
if news_context:
    for role in ["PM", "BA"]:
        prompt = f"다음 뉴스 목록을 바탕으로 {role} 직군에게 가장 중요한 전략적 액션 1가지를 제안해줘:\n{news_context}"
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        daily_report[f"{role.lower()}_brief"] = res.text

# 6. 아카이브 저장 로직
file_path = "data.json"
try:
    with open(file_path, "r", encoding="utf-8") as f: full_data = json.load(f)
except: full_data = []

full_data = [d for d in full_data if d['date'] != TODAY]
full_data.insert(0, daily_report)
with open(file_path, "w", encoding="utf-8") as f: json.dump(full_data, f, ensure_ascii=False, indent=2)
print(f"✅ {TODAY} 작업 완료!")
