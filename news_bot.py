import os
import json
import gspread
import time
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

# 4. 뉴스 수집 및 AI 분석 (안정성 강화 버전)
google_genai = genai.Client(api_key=GEMINI_KEY)
# ⭐ 테스트를 위해 max_results를 1개로 줄입니다 (가장 중요한 뉴스 하나만!)
google_news = GNews(language='ko', country='KR', period='1d', max_results=1)

daily_report = {"date": TODAY, "articles": [], "agent_brief": ""}
all_news_text = ""

for word in keywords:
    print(f"'{word}' 키워드 분석 중...")
    news_results = google_news.get_news(word)
    
    for news in news_results:
        try:
            # 💤 429 에러 방지를 위해 10초씩 넉넉히 쉽니다.
            time.sleep(10) 
            
            # 다시 2.0-flash 모델을 사용합니다 (404 방지)
            response = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"너는 모빌리티 전략가야. 이 뉴스를 PM 관점에서 1문장 요약해줘: {news['title']}"
            )
            
            if response.text:
                daily_report["articles"].append({
                    "keyword": word, 
                    "title": news['title'], 
                    "summary": response.text
                })
                all_news_text += f"[{word}] {news['title']}\n"
                print(f" - '{word}' 분석 성공!")
                
        except Exception as e:
            print(f" - '{word}' 분석 중 오류 발생: {e}")

# 🤖 Agentic AI Briefing (마지막 종합 분석)
if all_news_text:
    print("전체 브리핑 생성 중...")
    time.sleep(15) # 마지막 요청 전 충분히 휴식
    try:
        agent_response = google_genai.models.generate_content(
            model="gemini-2.0-flash", 
            contents=f"다음은 오늘의 모빌리티 뉴스 목록이야. 전체 트렌드를 파악해서 PM에게 오늘 가장 주목해야 할 핵심 이슈 1개와 권장 액션을 제안해줘:\n{all_news_text}"
        )
        daily_report["agent_brief"] = agent_response.text
    except Exception as e:
        print(f"브리핑 생성 실패: {e}")
        daily_report["agent_brief"] = "오늘의 뉴스를 종합 분석하는 중 오류가 발생했습니다."

# 5. 결과 저장 (Archive 방식)
file_path = "data.json"
try:
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            full_data = json.load(f)
    else:
        full_data = []
except:
    full_data = []

# 오늘 날짜 데이터 갱신
full_data = [d for d in full_data if d['date'] != TODAY]
full_data.insert(0, daily_report)

with open(file_path, "w", encoding="utf-8") as f:
    json.dump(full_data, f, ensure_ascii=False, indent=2)

print("--- 모든 작업이 정상적으로 완료되었습니다! ---")
