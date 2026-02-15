import os
import json
import gspread
from google import genai
from gnews import GNews
from oauth2client.service_account import ServiceAccountCredentials

# 1. 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_JSON = os.environ.get("GOOGLE_SHEETS_JSON")

# 2. 구글 시트 연결
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(GOOGLE_JSON)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("Mobility_Policy_Manager").sheet1 

# 3. 키워드 가져오기 (⭐빈 칸은 제외하는 필터 추가!)
raw_keywords = sheet.col_values(1)
keywords = [k for k in raw_keywords if k.strip()] # 글자가 있는 칸만 골라냄

print(f"수집 시작할 키워드 목록: {keywords}")

# 4. 뉴스 수집 및 AI 분석
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='1d', max_results=3)

report = []
for word in keywords:
    print(f"'{word}' 키워드 분석 중...")
    news_results = google_news.get_news(word)
    
    # 해당 키워드에 뉴스가 없을 경우 대비
    if not news_results:
        print(f"'{word}' 관련 최신 뉴스가 없습니다.")
        continue

    for news in news_results:
        try:
            response = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"다음 뉴스를 PM 관점에서 요약하고 시사점을 한 줄로 적어줘: {news['title']} - {news['description']}"
            )
            report.append({"keyword": word, "title": news['title'], "summary": response.text})
        except Exception as e:
            print(f"AI 분석 중 오류 발생: {e}")

# 5. 결과 저장
with open("data.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"최종 {len(report)}건의 뉴스 분석 완료!")
