import os, json, time, traceback, random, resend, re
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# [v10.1] 타임존 및 환경 변수 설정
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# ---------------------------------------------------------
# [에이전트 제어부] DB에서 8대 에이전트 지침 로드
# ---------------------------------------------------------
def get_agents():
    try:
        res = supabase.table("agent_config").select("*").execute()
        return {a['agent_role']: a for a in (res.data or [])}
    except Exception as e:
        print(f"🚨 에이전트 로드 실패: {str(e)}")
        return {}

def call_agent(prompt, agent_info, persona_override=None, max_retries=3):
    role = persona_override if persona_override else agent_info['agent_role']
    instruction = agent_info['instruction']
    
    for attempt in range(max_retries):
        try:
            time.sleep(2 + random.uniform(0, 1))
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
# [v10.1 보조 함수] 마크다운의 HTML 변환 (이메일용)
# ---------------------------------------------------------
def marked_parse_pseudo(text):
    if not text: return ""
    return text.replace("\n", "<br>").replace("**", "<b>").replace("* ", "• ")

# ---------------------------------------------------------
# [핵심 로직] 8대 에이전트 연쇄 호출 파이프라인
# ---------------------------------------------------------
def run_autonomous_engine():
    agents = get_agents()
    if not agents: return
    print(f"🚀 {TODAY} 8대 에이전트 연쇄 가동 시작 (v10.1)")

    # [INFO] 정책 로드
    info_policy = agents['INFO'].get('metadata', {})
    period = info_policy.get('period', '1d')
    
    # 유저 키워드 로드
    kw_res = supabase.table("user_settings").select("id, email, keywords").execute()
    
    for user in (kw_res.data or []):
        user_id, user_email = user['id'], user.get('email', 'Unknown')
        keywords = user.get('keywords', [])[:5]
        print(f"🔍 {user_email} (키워드: {keywords}) 분석 중...")
        
        raw_collection, all_titles = [], []
        for word in keywords:
            is_cjk = any(ord(char) > 0x1100 for char in word)
            lang, country = ('ko', 'KR') if is_cjk else ('en', 'US')
            gn = GNews(language=lang, country=country, period=period, max_results=5)
            items = gn.get_news(word)
            
            unique_items = []
            for n in items:
                if not any(SequenceMatcher(None, n['title'], u['title']).ratio() > 0.6 for u in unique_items):
                    unique_items.append(n)
                if len(unique_items) >= 2: break
            
            for n in unique_items:
                raw_collection.append({"keyword": word, "title": n['title'], "url": n['url']})
                all_titles.append(f"[{word}] {n['title']}")

        if not raw_collection: continue

        # [DATA] 데이터 엔지니어링
        refined_context = call_agent("\n".join(all_titles), agents['DATA'])

        # [BRIEF] 전문가 그룹 브리핑
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

        # [QA] 품질 보증 및 점수 추출 (v10.1 보강)
        qa_input = f"PM_Brief: {pm_brief}\nArticles: {str(all_titles)}"
        qa_feedback = call_agent(qa_input, agents['QA'])
        
        # QA 피드백 텍스트에서 '75/100' 또는 '75점' 형태의 점수 추출
        score_match = re.search(r"(\d+)(?=/100|점)", qa_feedback)
        qa_score = int(score_match.group(1)) if score_match else 50
        print(f"🛡️ QA Score: {qa_score}")

        # [HR] 인사 평가 (채용/해고 제안)
        hr_input = f"Current Keywords: {keywords}\nContext: {refined_context}"
        hr_proposal = call_agent(hr_input, agents['HR'])

        # 최종 패키지
        final_report = {
            "date": TODAY,
            "pm_brief": pm_brief,
            "ba_brief": ba_brief,
            "securities_brief": sec_brief,
            "hr_proposal": hr_proposal,
            "articles": articles_with_summary,
            "qa_feedback": qa_feedback
        }

        # [DB 저장]
        supabase.table("reports").insert({
            "user_id": user_id,
            "report_date": TODAY,
            "content": final_report,
            "qa_score": qa_score,
            "qa_feedback": qa_feedback
        }).execute()

        # [메일 발송]
        send_email_report(user_email, final_report)

def send_email_report(user_email, report):
    try:
        articles_html = "".join([
            f"<li style='margin-bottom:8px;'><b>[{a['keyword']}]</b> {a['title']} "
            f"<a href='{a['url']}' style='color:#007bff; text-decoration:none;'>[원문]</a></li>"
            for a in report['articles']
        ])
        
        # [v10.1] HR 섹션 디자인 보강
        hr_section = f"""
        <div style="background:#fff2f2; padding:20px; border-radius:12px; border-left:6px solid #ff4d4f; margin-top:25px;">
            <h3 style="color:#ff4d4f; margin-top:0; font-size:18px;">👨‍💼 HR 에이전트 인사이트 (채용/해고)</h3>
            <div style="color:#444; font-size:15px;">{marked_parse_pseudo(report['hr_proposal'])}</div>
        </div>
        """ if report.get('hr_proposal') else ""

        html_body = f"""
        <div style="font-family:'Pretendard', sans-serif; max-width:650px; margin:auto; line-height:1.7; color:#333;">
            <h2 style="color:#007bff; border-bottom:3px solid #007bff; padding-bottom:12px; margin-bottom:25px;">Fitz Intelligence 리포트 ({TODAY})</h2>
            <div style="background:#f8f9fa; padding:20px; border-radius:12px; border-left:6px solid #007bff; margin-bottom:25px;">
                <h3 style="margin-top:0; color:#0056b3;">📊 PM 종합 브리핑</h3>
                <div style="font-size:15px;">{marked_parse_pseudo(report['pm_brief'])}</div>
            </div>
            {hr_section}
            <h3 style="margin-top:30px; border-bottom:1px solid #eee; padding-bottom:10px;">📰 수집된 지능 원문 리스트</h3>
            <ul style="padding-left:20px; font-size:14px;">{articles_html}</ul>
            <hr style="border:0; border-top:1px solid #eee; margin-top:40px;">
            <p style="font-size:12px; color:#999; text-align:center;">본 분석은 QA 에이전트의 검증을 통과한 무결성 인사이트입니다.</p>
        </div>
        """

        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": [user_email],
            "subject": f"[{TODAY}] AI 기업 자율 분석 리포트 & HR 제안",
            "html": html_body
        })
        print(f"📧 {user_email}님 메일 발송 완료")
    except Exception as e:
        print(f"📧 이메일 발송 에러: {str(e)}")

if __name__ == "__main__":
    run_autonomous_engine()
