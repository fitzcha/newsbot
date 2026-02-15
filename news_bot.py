import os
import json
import gspread
from google import genai
from gnews import GNews
from oauth2client.service_account import ServiceAccountCredentials

# 1. 환경 설정 (금고에서 열쇠 꺼내기)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_JSON = os.environ.get("GOOGLE_SHEETS_JSON")

# 2. 구글 시트 연결
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(GOOGLE_JSON)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
# 시트 제목이 Mobility_Policy_Manager가 맞는지 확인하세요!
sheet = client.open("Mobility_Policy_Manager").sheet1 

# 3. 키워드 가져오기
keywords = sheet.col_values(1) 

# 4. 뉴스 수집 및 AI 분석
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='1d', max_results=3)

report = []
for word in keywords:
    news_results = google_news.get_news(word)
    for news in news_results:
        response = google_genai.models.generate_content(
            model="gemini-2.0-flash", 
            contents=f"다음 뉴스를 PM 관점에서 요약하고 시사점을 한 줄로 적어줘: {news['title']} - {news['description']}"
        )
        report.append({"keyword": word, "title": news['title'], "summary": response.text})

# 5. 결과 저장
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print("분석 완료!")
