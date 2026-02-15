import os
import json
import gspread
import time  # 👈 시간 지연을 위해 추가
from google import genai
from gnews import GNews
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# 1. 환경 설정
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_JSON = os.environ.get("GOOGLE_SHEETS_JSON")
TODAY = datetime.now().strftime("%Y-%m-%d")

# 2. 구글 시트 연결
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(GOOGLE_JSON)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("Mobility_Policy_Manager").sheet1 

# 3. 키워드 가져오기
raw_keywords = sheet.col_values(1)
keywords = [k for k in raw_keywords if k.strip()]

# 4. 뉴스 수집 및 Agentic AI 분석
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='1d', max_results=3)

daily_report = {"date": TODAY, "articles": [], "agent_brief": ""}
all_news_text = ""

for word in keywords:
    print(f"'{word}' 키워드 분석 중...")
    news_results = google_news.get_news(word)
    for news in news_results:
        try:
            # 💤 너무 빨리 요청하지 않도록 3초씩 쉬어갑니다.
            time.sleep(3) 
            
            response = google_genai.models.generate_content(
                model="gemini-1.5-flash", 
                contents=f"너는 모빌리티 전략가야. 이 뉴스를 PM 관점에서 1문장 요약해줘: {news['title']}"
            )
            daily_report["articles"].append({"keyword": word, "title": news['title'], "summary": response.text})
            all_news_text += f"[{word}] {news['title']}\n"
        except Exception as e:
            print(f"요류 발생: {e}")

# 🤖 Agentic AI Briefing 생성 전에도 잠시 쉽니다.
time.sleep(5)
if all_news_text:
    agent_response = google_genai.models.generate_content(
        model="gemini-1.5-flash", 
        contents=f"다음은 오늘의 모빌리티 뉴스 목록이야. 전체 트렌드를 파악해서 PM에게 오늘 가장 주목해야 할 핵심 이슈 1개와 권장 액션을 제안해줘:\n{all_news_text}"
    )
    daily_report["agent_brief"] = agent_response.text

# 5. 결과 저장
file_path = "data.json"
if os.path.exists(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            full_data = json.load(f)
        except:
            full_data = []
else:
    full_data = []

full_data = [d for d in full_data if d['date'] != TODAY]
full_data.insert(0, daily_report)

with open(file_path, "w", encoding="utf-8") as f:
    json.dump(full_data, f, ensure_ascii=False, indent=2)

print("분석 완료 및 저장 성공!")
