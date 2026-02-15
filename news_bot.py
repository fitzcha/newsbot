import os
import json
import gspread
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
keywords = [k for k in sheet.col_values(1) if k.strip()]

# 4. 뉴스 수집 및 Agentic AI 분석
google_genai = genai.Client(api_key=GEMINI_KEY)
google_news = GNews(language='ko', country='KR', period='1d', max_results=3)

daily_report = {"date": TODAY, "articles": [], "agent_brief": ""}
all_news_text = ""

for word in keywords:
    news_results = google_news.get_news(word)
    for news in news_results:
        response = google_genai.models.generate_content(
            model="gemini-2.0-flash", 
            contents=f"너는 모빌리티 전략가야. 이 뉴스를 PM 관점에서 1문장 요약해줘: {news['title']}"
        )
        daily_report["articles"].append({"keyword": word, "title": news['title'], "summary": response.text})
        all_news_text += f"[{word}] {news['title']}\n"

# 🤖 Agentic AI Step: 오늘의 전체 브리핑 생성
if all_news_text:
    agent_response = google_genai.models.generate_content(
        model="gemini-2.0-flash", 
        contents=f"다음은 오늘의 모빌리티 뉴스 목록이야. 전체 트렌드를 파악해서 PM에게 오늘 가장 주목해야 할 핵심 이슈 1개와 권장 액션을 제안해줘:\n{all_news_text}"
    )
    daily_report["agent_brief"] = agent_response.text

# 5. 기존 데이터와 합쳐서 저장 (Archive 방식)
file_path = "data.json"
if os.path.exists(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        full_data = json.load(f)
else:
    full_data = []

# 중복 날짜 방지 후 추가
full_data = [d for d in full_data if d['date'] != TODAY]
full_data.insert(0, daily_report)

with open(file_path, "w", encoding="utf-8") as f:
    json.dump(full_data, f, ensure_ascii=False, indent=2)

# ⭐ 키워드 목록도 별도 저장 (UI에서 태그로 보여주기 위해)
with open("keywords.json", "w", encoding="utf-8") as f:
    json.dump(keywords, f, ensure_ascii=False, indent=2)
