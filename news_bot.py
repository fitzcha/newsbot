import os, json, time, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher # 유사도 계산용

# 1. 환경 설정 및 클라이언트 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

MASTER_EMAIL = "positivecha@gmail.com"
TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# [개선] 유사도 필터링을 위해 우선 10개를 가져온 뒤 5개를 엄선합니다.
google_news = GNews(language='ko', country='KR', period='2d', max_results=10)

def is_similar(a, b):
    """문자열 유사도를 0~1 사이로 반환합니다."""
    return SequenceMatcher(None, a, b).ratio()

def analyze_news(title, role="PM"):
    """성환님의 PM/BA 페르소나 분석 로직을 유지합니다."""
    prompt = f"당신은 {role}입니다. 뉴스 '{title}'을 3개 불릿 포인트로 요약하고 인사이트를 주십시오."
    try:
        res = google_genai.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return res.text
    except:
        return "• 분석 중 오류 발생"

def run_engine():
    # 모든 사용자 가져오기
    users_res = supabase.table("users").select("*").execute()
    users = users_res.data
    
    print(f"📡 v5.6 엔진 가동: 총 {len(users)}명 대상 중복 제거 및 5개 기사 분석 시작")

    for user in users:
        user_id = user['id']
        user_email = user['email']
        
        # 유저별 활성화된 키워드 가져오기
        kw_res = supabase.table("keywords").select("word").eq("user_id", user_id).eq("is_active", True).execute()
        user_keywords = [k['word'] for k in kw_res.data]
        
        if not user_keywords:
            print(f"⏩ {user_email}님은 설정된 키워드가 없어 건너뜁니다.")
            continue

        print(f"🔍 {user_email}님 분석 시작 (중복 제외 최대 5개 선별)")
        user_report = {
            "date": TODAY, 
            "articles": [], 
            "pm_brief": "", 
            "ba_brief": "", 
            "tracked_keywords": user_keywords
        }
