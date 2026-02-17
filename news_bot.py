import os, json, time, traceback, random, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime
from difflib import SequenceMatcher

# 1. 환경 설정 및 클라이언트 초기화
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY") # 이메일 발송용

TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

ROLES = {
    "HR": "인사 결정권자. 성과 평가 기반 채용/해고 제안.",
    "BA_INTERNAL": "플랫폼 감사관. 의사결정 품질 비판.",
    "DEBUGGER": "시스템 엔지니어. 코드 오류 분석 및 패치 제안.",
    "PM": "IT 서비스 기획자", "BA": "전략 분석가", "SEC": "증권 분석가"
}

# [v8.9 추가] 이메일 발송 로직
def send_email_report(user_email, report_data):
    """분석된 리포트를 사용자 이메일로 발송"""
    try:
        # 뉴스 항목들을 HTML 리스트로 변환
        articles_html = "".join([
            f"<li><b>[{a['keyword']}] {a['title']}</b><br>{a['pm_summary'][:200]}... <a href='{a['url']}'>원문보기</a></li><br>"
            for a in report_data['articles']
        ])
        
        # HTML 메일 본문 구성
        html_content = f"""
        <div style="font-family: sans-serif; line-height: 1.6; color: #333;">
            <h2>🚀 {TODAY} Fitz Intelligence 데일리 리포트</h2>
            <p>안녕하세요, {user_email.split('@')[0]}님! 오늘 아침의 분석 결과입니다.</p>
            <hr>
            <h3>📊 종합 브리핑 (PM 시각)</h3>
            <div style="background: #f4f4f4; padding: 15px; border-radius: 8px;">{report_data['pm_brief']}</div>
            <h3>📰 주요 뉴스 요약</h3>
            <ul>{articles_html}</ul>
            <hr>
            <p>더 자세한 분석과 거버넌스 결정은 <a href="https://newsbot-smoky.vercel.app">플랫폼</a>에서 확인하세요.</p>
        </div>
        """
        
        # Resend를 통해 메일 발송
        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>", # 추후 도메인 연결 시 변경 가능
            "to": user_email,
            "subject": f"[{TODAY}] 오늘의 지능형 뉴스 분석 리포트가 도착했습니다.",
            "html": html_content
        })
        print(f"📧 {user_email}님에게 이메일 리포트 발송 완료.")
    except Exception as e:
        print(f"🚨 이메일 발송 실패 ({user_email}): {str(e)}")

def call_agent(prompt, role_key, max_retries=3):
    """429 에러 방지를 위한 적응형 호출 로직"""
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            time.sleep(5 + random.uniform(0, 2)) 
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
        except Exception as e:
            if "429" in str(e):
                time.sleep((2 ** attempt) * 20)
            else: raise e
    return "• 분석 지연"

def execute_governance():
    """23:30 의사결정 확정 및 타임락 집행"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    res = supabase.table("pending_approvals").select("*").neq("status", "EXECUTED").execute()
    decisions = res.data if res.data else []
    
    for p in decisions:
        if now >= deadline or p['status'] in ['APPROVED', 'REJECTED']:
            print(f"🔒 결정 확정: {p['word']} ({p['status']})")
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], "action_type": p['type'], "target_word": p['word'],
                "execution_method": "AUTO_FINALIZER", "details": "23:30 타임락 확정"
            }).execute()

def run_main_engine():
    settings_res = supabase.table("user_settings").select("*").execute()
    settings = settings_res.data if settings_res.data else []
    
    for user_set in settings:
        user_id = user_set['id']
        user_email = user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5]
        
        if not user_keywords: continue
        print(f"🔍 {user_email}님 분석 시작: {user_keywords}")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            
            # 1일 전 뉴스를 기본으로 하되, 없으면 3일로 확장
            gn = GNews(language=lang, country=country, period='1d', max_results=10)
            items = gn.get_news(word)

            if not items:
                gn_extended = GNews(language=lang, country=country, period='3d', max_results=10)
                items = gn_extended.get_news(word)

            unique_news = []
            for n in items:
                if any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_news): continue
                unique_news.append(n)
                if len(unique_news) >= 3: break

            for n in unique_news:
                article = {
                    "keyword": word, "title": n['title'], "url": n['url'],
                    "pm_summary": call_agent(n['title'], "PM"),
                    "ba_summary": call_agent(n['title'], "BA"),
                    "sec_summary": call_agent(n['title'], "SEC")
                }
                report["articles"].append(article)
                all_titles.append(f"[{word}] {n['title']}")

        if report["articles"]:
            context = "\n".join(all_titles)
            report["pm_brief"] = call_agent(context, "PM")
            report["ba_brief"] = call_agent(context, "BA")
            report["securities_brief"] = call_agent(context, "SEC")
            report["internal_audit"] = call_agent("품질 비판", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 평가", "HR")
            
            # 1. DB 저장
            supabase.table("reports").insert({"user_id": user_id, "report_date": TODAY, "content": report}).execute()
            print(f"✅ {user_email}님 리포트 DB 저장 성공.")
            
            # 2. [v8.9] 이메일 발송
            send_email_report(user_email, report)
        else:
            print(f"⚠️ {user_email}님 분석 가능한 뉴스 없음.")

if __name__ == "__main__":
    try:
        execute_governance()
        run_main_engine()
    except Exception as e:
        print(f"🚨 시스템 오류: {traceback.format_exc()}")
