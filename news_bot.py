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
resend.api_key = os.environ.get("RESEND_API_KEY") # 이메일 발송용 API 키

TODAY = datetime.now().strftime("%Y-%m-%d")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# 페르소나 정의 (인사권자, 감사관, 기획자 등)
ROLES = {
    "HR": "인사 결정권자. 키워드 성과 평가 및 채용/해고 제안.",
    "BA_INTERNAL": "플랫폼 내부 감사관. 전략적 결함 및 품질 비판.",
    "DEBUGGER": "시스템 엔지니어. 코드 안정성 분석.",
    "PM": "IT 서비스 기획자", 
    "BA": "전략 분석가", 
    "SEC": "증권 분석가"
}

# [v9.2] 이메일 발송 모듈 (Resend 기반)
def send_email_report(user_email, report_data):
    """사용자별 맞춤 HTML 리포트 이메일 발송"""
    try:
        # 뉴스 항목 HTML 구성
        articles_html = "".join([
            f"<li style='margin-bottom:15px;'><b>[{a['keyword']}] {a['title']}</b><br>"
            f"<span style='color:#666; font-size:0.9em;'>{a['pm_summary'][:150]}...</span> "
            f"<a href='{a['url']}' style='color:#007bff; text-decoration:none;'>원문보기</a></li>"
            for a in report_data['articles']
        ])
        
        html_content = f"""
        <div style="font-family: sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: auto;">
            <h2 style="color: #007bff;">🚀 {TODAY} Fitz Intelligence Report</h2>
            <p>안녕하세요, {user_email.split('@')[0]}님! 오늘의 지능형 뉴스 분석 리포트입니다.</p>
            <hr style="border: 0; border-top: 1px solid #eee;">
            <h3 style="background: #f8f9fa; padding: 10px; border-radius: 5px;">📊 PM 종합 브리핑</h3>
            <div style="padding: 0 10px;">{report_data['pm_brief']}</div>
            <h3 style="margin-top:25px;">📰 주요 뉴스 리스트</h3>
            <ul style="padding-left: 20px;">{articles_html}</ul>
            <p style="font-size: 0.8em; color: #999; margin-top: 30px; border-top: 1px solid #eee; padding-top: 10px;">
                본 리포트는 Fitz Intelligence AI에 의해 자동 생성되었습니다. 
                <a href="https://newsbot-smoky.vercel.app">플랫폼 바로가기</a>
            </p>
        </div>
        """
        
        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": user_email,
            "subject": f"[{TODAY}] 오늘의 뉴스 인사이트가 배달되었습니다.",
            "html": html_content
        })
        print(f"📧 {user_email}님 이메일 발송 성공.")
    except Exception as e:
        print(f"🚨 이메일 발송 오류 ({user_email}): {str(e)}")

# [v9.2] AI 에이전트 호출 (429 에러 방지 포함)
def call_agent(prompt, role_key, max_retries=3):
    persona = ROLES.get(role_key, "전문가")
    for attempt in range(max_retries):
        try:
            # RPM 조절을 위한 휴식
            time.sleep(5 + random.uniform(0, 2)) 
            res = google_genai.models.generate_content(
                model="gemini-2.0-flash", 
                contents=f"당신은 {persona}입니다.\n{prompt}"
            )
            return res.text
        except Exception as e:
            if "429" in str(e) or "Quota" in str(e):
                wait_time = (2 ** attempt) * 20 + random.uniform(0, 5)
                print(f"⚠️ {role_key} 과부하. {wait_time:.1f}초 후 재시도...")
                time.sleep(wait_time)
            else: raise e
    return "• 분석 지연 (데이터 확보 실패)"

# [v9.2] 거버넌스 타임락 집행 (23:30 확정)
def execute_governance():
    """의사결정 데드라인 확인 및 잠금 처리"""
    now = datetime.now()
    deadline = now.replace(hour=23, minute=30, second=0, microsecond=0)
    
    # EXECUTED가 아닌 모든 결정 사항 조회
    res = supabase.table("pending_approvals").select("*").neq("status", "EXECUTED").execute()
    active_decisions = res.data if res.data else []
    
    for p in active_decisions:
        # 시간 초과 혹은 수기 결정(승인/반려) 완료 시 잠금
        if now >= deadline or p['status'] in ['APPROVED', 'REJECTED']:
            print(f"🔒 결정 최종 확정: {p['word']} ({p['status']})")
            supabase.table("pending_approvals").update({"status": "EXECUTED"}).eq("id", p['id']).execute()
            
            # 히스토리 로그 기록
            supabase.table("action_logs").insert({
                "user_id": p['user_id'], 
                "action_type": p['type'], 
                "target_word": p['word'],
                "execution_method": "AUTO_SYSTEM",
                "details": "23:30 데드라인 통과에 따른 자동 확정"
            }).execute()

# [v9.2] 메인 엔진: 유저별 뉴스 수집 및 분석
def run_main_engine():
    # user_settings 테이블에서 유저별 설정 로드
    settings_res = supabase.table("user_settings").select("*").execute()
    settings = settings_res.data if settings_res.data else []
    
    for user_set in settings:
        user_id = user_set['id']
        user_email = user_set.get('email', 'Unknown')
        user_keywords = user_set.get('keywords', [])[:5] # 최대 5개 유지
        
        if not user_keywords:
            print(f"⏩ {user_email}님 설정 키워드 없음. 스킵.")
            continue

        print(f"🔍 {user_email}님 정밀 분석 시작: {user_keywords}")
        report = {"date": TODAY, "articles": [], "tracked_keywords": user_keywords}
        all_titles = []

        for word in user_keywords:
            # CJK 판별 및 언어 맞춤 검색
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            
            # 1차 시도: 1일(1d) 최신 뉴스
            gn = GNews(language=lang, country=country, period='1d', max_results=10)
            items = gn.get_news(word)

            # 2차 시도: 결과 없으면 3일치로 확장
            if not items:
                print(f"🔄 {word} (1d) 결과 없음. 기간 확장 중...")
                gn_ext = GNews(language=lang, country=country, period='3d', max_results=10)
                items = gn_ext.get_news(word)

            unique_news = []
            for n in items:
                # 성환님의 0.6 유사도 필터링 규칙 적용
                if any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_news):
                    continue
                unique_news.append(n)
                if len(unique_news) >= 3: break # 키워드당 정예 3건 추출

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
            # 종합 인사이트 및 거버넌스 제안 생성
            context = "\n".join(all_titles)
            report["pm_brief"] = call_agent(context, "PM")
            report["ba_brief"] = call_agent(context, "BA")
            report["securities_brief"] = call_agent(context, "SEC")
            report["internal_audit"] = call_agent("플랫폼 분석 품질 비판", "BA_INTERNAL")
            report["hr_proposal"] = call_agent(f"키워드 {user_keywords} 성과 기반 해고/채용 제안", "HR")
            
            # DB 저장 및 이메일 전송
            supabase.table("reports").insert({
                "user_id": user_id, 
                "report_date": TODAY, 
                "content": report
            }).execute()
            
            send_email_report(user_email, report)
            print(f"✅ {user_email}님 최종 프로세스 완료.")
        else:
            print(f"⚠️ {user_email}님 분석 가능한 뉴스 데이터가 없습니다.")

if __name__ == "__main__":
    try:
        # 1. 의사결정 데드라인 집행
        execute_governance()
        # 2. 뉴스 수집 및 분석 가동
        run_main_engine()
    except Exception as e:
        print(f"🚨 시스템 치명적 오류: {traceback.format_exc()}")
