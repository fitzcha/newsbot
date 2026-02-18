import os, json, time, traceback, random, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# [v10.0] 타임존 및 환경 변수 설정
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# ---------------------------------------------------------
# [에이전트 제어부] DB에서 8대 에이전트 지침 및 설정 로드
# ---------------------------------------------------------
def get_agents():
    """agent_config 테이블에서 8대 에이전트의 뇌(Prompt/Params)를 로드합니다."""
    try:
        res = supabase.table("agent_config").select("*").execute()
        return {a['agent_role']: a for a in (res.data or [])}
    except Exception as e:
        print(f"🚨 에이전트 로드 실패: {str(e)}")
        return {}

def call_agent(prompt, agent_info, persona_override=None, max_retries=3):
    """DB 설정값(Temperature, Model)을 기반으로 개별 에이전트를 가동합니다."""
    role = persona_override if persona_override else agent_info['agent_role']
    instruction = agent_info['instruction']
    
    for attempt in range(max_retries):
        try:
            time.sleep(2 + random.uniform(0, 1)) # RPM 조절
            res = google_genai.models.generate_content(
                model=agent_info.get('model_name', 'gemini-2.0-flash'),
                contents=f"당신은 {role}입니다.\n지침: {instruction}\n\n입력 데이터: {prompt}",
                config={'temperature': agent_info.get('temperature', 0.7)}
            )
            return res.text
        except Exception as e:
            if "429" in str(e):
                wait = (2 ** attempt) * 20
                print(f"⚠️ {role} 과부하 대기: {wait}초")
                time.sleep(wait)
            else: raise e
    return f"• {role} 분석 지연"

# ---------------------------------------------------------
# [핵심 로직] 8대 에이전트 연쇄 호출 파이프라인
# ---------------------------------------------------------
def run_autonomous_engine():
    # 1. 에이전트 세팅 로드
    agents = get_agents()
    if not agents: return
    print(f"🚀 {TODAY} 8대 에이전트 연쇄 가동 시작")

    # 2. [INFO] 정보수집 정책 결정 및 키워드(직원) 리스트 확보
    info_policy = agents['INFO'].get('metadata', {})
    period = info_policy.get('period', '1d')
    
    # [KW] 키워드 에이전트 관점의 직원 리스트 로드
    kw_res = supabase.table("user_settings").select("id, email, keywords").execute()
    
    for user in (kw_res.data or []):
        user_id, user_email = user['id'], user.get('email', 'Unknown')
        keywords = user.get('keywords', [])[:5]
        
        print(f"🔍 {user_email} (직원수: {len(keywords)}) 분석 중...")
        raw_collection = []
        all_titles = []

        # 3. [INFO] 실제 뉴스 수집 실행
        for word in keywords:
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            gn = GNews(language=lang, country=country, period=period, max_results=5)
            items = gn.get_news(word)
            
            # 중복 제거 (v8.8 로직 유지)
            unique_items = []
            for n in items:
                if not any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_items):
                    unique_items.append(n)
                if len(unique_items) >= 2: break
            
            for n in unique_items:
                raw_collection.append({"keyword": word, "title": n['title'], "url": n['url']})
                all_titles.append(f"[{word}] {n['title']}")

        if not raw_collection: continue

        # 4. [DATA] 데이터 엔지니어링: 뉴스 정제 및 분석용 컨텍스트 생성
        context_data = "\n".join(all_titles)
        refined_context = call_agent(context_data, agents['DATA'])

        # 5. [BRIEF] 전문가 그룹(PM/BA/SEC) 브리핑 작성
        articles_with_summary = []
        for news in raw_collection:
            articles_with_summary.append({
                **news,
                "pm_summary": call_agent(news['title'], agents['BRIEF'], "PM"),
                "ba_summary": call_agent(news['title'], agents['BRIEF'], "BA"),
                "sec_summary": call_agent(news['title'], agents['BRIEF'], "SEC")
            })

        pm_brief = call_agent(refined_context, agents['BRIEF'], "PM")
        ba_brief = call_agent(refined_context, agents['BRIEF'], "BA")
        sec_brief = call_agent(refined_context, agents['BRIEF'], "SEC")

        # 6. [QA] 품질 보증: 리포트 검수 및 점수 부여
        qa_input = f"Briefing: {pm_brief}\nArticles: {str(all_titles)}"
        qa_feedback = call_agent(qa_input, agents['QA'])
        # 간단한 점수 추출 로직 (지침에 'Score: 00' 포함 권장)
        qa_score = 80 if "통과" in qa_feedback or "Good" in qa_feedback else 50

        # 7. [HR] 인사 평가: 키워드(직원) 성과 기반 해고/채용 제안
        hr_input = f"Keywords: {keywords}\nPerformance Data: {refined_context}"
        hr_proposal = call_agent(hr_input, agents['HR'])

        # 8. 최종 리포트 패키징 (v8.8 FE 호환 구조 유지)
        final_report = {
            "date": TODAY,
            "pm_brief": pm_brief,
            "ba_brief": ba_brief,
            "securities_brief": sec_brief,
            "hr_proposal": hr_proposal,
            "articles": articles_with_summary,
            "qa_feedback": qa_feedback
        }

        # 9. [DB 저장] QA 점수 포함
        supabase.table("reports").insert({
            "user_id": user_id,
            "report_date": TODAY,
            "content": final_report,
            "qa_score": qa_score,
            "qa_feedback": qa_feedback
        }).execute()

        # 10. 이메일 발송
        send_email_report(user_email, final_report)

def send_email_report(user_email, report):
    try:
        articles_html = "".join([
            f"<li><b>[{a['keyword']}]</b> {a['title']} <a href='{a['url']}'>[원문]</a></li>"
            for a in report['articles']
        ])
        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": [user_email],
            "subject": f"[{TODAY}] AI 기업 자율 분석 리포트",
            "html": f"<h2>🚀 {TODAY} 리포트</h2>{report['pm_brief']}<h3>📰 수집 뉴스</h3><ul>{articles_html}</ul>"
        })
    except Exception as e: print(f"📧 메일 실패: {str(e)}")

if __name__ == "__main__":
    run_autonomous_engine()
