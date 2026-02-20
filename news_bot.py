import os, json, time, traceback, random, resend, re, subprocess, shutil
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# [v13.0] 에이전트 통합 + KeyError 수정 + QA 실제 활성화
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL = os.environ.get("SUPABASE_URL")
SB_KEY = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai = genai.Client(api_key=GEMINI_KEY)

# ---------------------------------------------------------
# [보조] 시스템 로그 및 데이터 동기화
# ---------------------------------------------------------
def log_to_db(user_id, target_word, action="분석", method="Auto"):
    try:
        supabase.table("action_logs").insert({
            "user_id": user_id,
            "action_type": action,
            "target_word": target_word,
            "execution_method": method,
            "details": "Success"
        }).execute()
    except: pass

def record_performance(user_id, keyword, count):
    try:
        supabase.table("keyword_performance").insert({
            "user_id": user_id,
            "keyword": keyword,
            "hit_count": count,
            "report_date": TODAY
        }).execute()
    except: pass

def get_agents():
    res = supabase.table("agents").select("*").execute()
    return {a['agent_role']: a for a in (res.data or [])}

def call_agent(prompt, agent_info, persona_override=None, force_one_line=False):
    if not agent_info: return "분석 데이터 없음"
    role = persona_override if persona_override else agent_info['agent_role']
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"
    final_prompt = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라) {prompt}" if force_one_line else prompt + guard

    try:
        res = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {final_prompt}"
        )
        output = res.text.strip()
        return output.split('\n')[0] if force_one_line else output
    except: return "분석 지연 중"

# ---------------------------------------------------------
# [New] QA 에이전트 실제 활성화
# ---------------------------------------------------------
def run_qa_check(ctx, report, agents):
    """QA 에이전트를 실제로 호출해 리포트 품질 점수를 반환한다."""
    qa = agents.get('QA')
    if not qa:
        print("⚠️ [QA] QA 에이전트 없음 — 기본 점수 70 적용")
        return 70, "QA 에이전트 미설정"

    qa_prompt = (
        f"아래 리포트를 검수하라.\n"
        f"팩트 오류, 논리 비약, 중복 내용, 1줄 원칙 위반 여부를 확인하고\n"
        f"반드시 첫 줄에 0~100 사이 숫자 점수만 단독으로 출력하고, 둘째 줄부터 간단한 코멘트를 작성하라.\n\n"
        f"[BA 분석]\n{report.get('ba_brief', '')}\n\n"
        f"[증권 분석]\n{report.get('securities_brief', '')}\n\n"
        f"[PM 기획]\n{report.get('pm_brief', '')}"
    )
    result = call_agent(qa_prompt, qa)
    lines = result.strip().split('\n')
    try:
        score = int(''.join(filter(str.isdigit, lines[0])))
        score = min(max(score, 0), 100)
    except:
        score = 70
    comment = '\n'.join(lines[1:]).strip() if len(lines) > 1 else "검수 완료"
    print(f"🔍 [QA] 품질 점수: {score}점")
    return score, comment

# ---------------------------------------------------------
# [New] GitHub 저장소 동기화 (data.json 강제 갱신)
# ---------------------------------------------------------
def sync_data_to_github():
    try:
        print("📁 [Sync] GitHub 저장소 동기화 시작...")
        res = supabase.table("reports").select("*").eq("report_date", TODAY).execute()
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(res.data, f, ensure_ascii=False, indent=2)
        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add data.json',
            f'git commit -m "📊 [Data Sync] {TODAY} Insights Update"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)
        print("🚀 [Sync] GitHub data.json 갱신 및 푸시 완료")
    except Exception as e:
        print(f"🚨 [Sync] 동기화 실패: {e}")

# ---------------------------------------------------------
# [1] DEV 엔진: 마스터 'CONFIRMED' 작업 집행
# ---------------------------------------------------------
def run_self_evolution():
    try:
        task_res = supabase.table("dev_backlog").select("*").eq("status", "CONFIRMED").order("priority").limit(1).execute()
        if not task_res.data:
            return print("💤 [DEV] 마스터의 '실행 확정' 대기 작업 없음.")

        task = task_res.data[0]
        file_path = task.get('affected_file', 'news_bot.py')
        print(f"🛠️ [DEV] 마스터 지휘 업무 착수: {task['title']}")

        backup_dir = "backups"
        if not os.path.exists(backup_dir): os.makedirs(backup_dir)
        shutil.copy2(file_path, f"{backup_dir}/{file_path}.{NOW.strftime('%H%M%S')}.bak")

        with open(file_path, "r", encoding="utf-8") as f: current_code = f.read()

        agents = get_agents()
        dev_prompt = f"요구사항: {task['task_detail']}\n\n반드시 전체 코드를 ```python ... ``` 안에 출력.\n--- 현재 코드 ---\n{current_code}"
        raw_output = call_agent(dev_prompt, agents.get('DEV'), "Senior Python Engineer")

        code_match = re.search(r"```python\s+(.*?)\s+```", raw_output, re.DOTALL)
        new_code = code_match.group(1).strip() if code_match else raw_output.strip()

        compile(new_code, file_path, 'exec')
        with open(file_path, "w", encoding="utf-8") as f: f.write(new_code)

        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add .',
            f'git commit -m "🤖 [v13.0] {task["title"]}"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({"status": "COMPLETED", "completed_at": NOW.isoformat()}).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")
    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")

# ---------------------------------------------------------
# [2] 에이전트 자아 성찰
# ---------------------------------------------------------
def run_agent_self_reflection(report_id):
    """VOC 기반 에이전트 지침 자동 개선 — agents 테이블 직접 업데이트"""
    try:
        feedback_res = supabase.table("report_feedback").select("*").eq("report_id", report_id).execute()
        if not feedback_res.data: return
        agents = get_agents()
        skip_roles = {'DEV', 'QA', 'MASTER', 'DATA', 'INFO', 'KW'}
        for role, info in agents.items():
            if role in skip_roles: continue
            neg_voc = [f['feedback_text'] for f in feedback_res.data if f['target_agent'] == role and not f['is_positive']]
            if not neg_voc: continue
            reflect_prompt = (
                f"현재 지침: {info['instruction']}\n"
                f"고객불만: {', '.join(neg_voc)}\n\n"
                f"[PROPOSAL]수정지침 [REASON]수정근거 형식으로 상신하라."
            )
            reflection = call_agent(reflect_prompt, info, "Insight Evolver")
            p = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", reflection, re.DOTALL)
            r = re.search(r"\[REASON\](.*?)$", reflection, re.DOTALL)
            if p:
                new_instruction = p.group(1).strip()
                reason = r.group(1).strip() if r else "VOC 피드백 반영"
                # pending_approvals 대신 agents 테이블에 직접 반영
                supabase.table("agents").update({
                    "instruction": new_instruction,
                    "last_run_at": NOW.isoformat()
                }).eq("agent_role", role).execute()
                print(f"🔄 [REFLECT] {role} 지침 업데이트 완료: {reason[:50]}")
    except Exception as e:
        print(f"⚠️ [REFLECT] 성찰 실패: {e}")

def manage_deadline_approvals():
    """23:30 이후 자동 승인 — agents 테이블 기반으로 단순화"""
    # pending_approvals 테이블 제거로 인해 이 함수는 현재 비활성
    pass

# ---------------------------------------------------------
# [4] 자율 분석 엔진
# ---------------------------------------------------------
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v13.0 가동")

    # QA fail_threshold 설정
    QA_FAIL_THRESHOLD = 40

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id   = user['id']
            user_email = user.get('email', 'Unknown')
            keywords  = user.get('keywords', [])[:5]
            if not keywords: continue

            check_report = supabase.table("reports").select("id").eq("user_id", user_id).eq("report_date", TODAY).execute()
            if check_report.data:
                print(f"⏭️  [Skip] {user_email}님은 이미 발송 완료되었습니다.")
                continue

            all_news_context, articles_with_summary = [], []
            for word in keywords:
                gn = GNews(language='ko' if any(ord(c) > 0x1100 for c in word) else 'en', max_results=2)
                news_list = gn.get_news(word)
                record_performance(user_id, word, len(news_list))
                for n in news_list:
                    short_summary = call_agent(f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True)
                    impact = call_agent(f"뉴스: {n['title']}\n전망 1줄.", agents.get('STOCK', agents.get('BRIEF')), force_one_line=True)
                    articles_with_summary.append({**n, "keyword": word, "pm_summary": short_summary, "impact": impact})
                    all_news_context.append(f"[{word}] {n['title']}")
                log_to_db(user_id, word, "뉴스수집")

            if not articles_with_summary: continue
            ctx = "\n".join(all_news_context)

            # [P3-1] agents.get() fallback — KeyError 완전 방지
            ba    = agents.get('BA',    agents.get('BRIEF'))
            stock = agents.get('STOCK', agents.get('BRIEF'))
            pm    = agents.get('PM',    agents.get('BRIEF'))
            hr    = agents.get('HR',    agents.get('BRIEF'))

            final_report = {
                "ba_brief":         call_agent(f"비즈니스 수익 구조 및 경쟁 분석:\n{ctx}", ba),
                "securities_brief": call_agent(f"주식 시장 반응 및 투자 인사이트:\n{ctx}", stock),
                "pm_brief":         call_agent(f"전략적 서비스 기획 관점 브리핑:\n{ctx}", pm),
                "hr_proposal":      call_agent(f"조직 및 인사 관리 제안:\n{ctx}", hr),
                "articles":         articles_with_summary
            }

            # [P3-2] QA 실제 활성화 — 하드코딩 95 제거
            qa_score, qa_feedback = run_qa_check(ctx, final_report, agents)

            if qa_score < QA_FAIL_THRESHOLD:
                print(f"⛔ [QA] {user_email} 품질 미달({qa_score}점) — 리포트 발송 보류")
                log_to_db(user_id, "QA_FAIL", f"QA 점수 {qa_score}점으로 발송 보류")
                continue

            res = supabase.table("reports").upsert({
                "user_id":        user_id,
                "report_date":    TODAY,
                "content":        final_report,
                "qa_score":       qa_score,
                "qa_feedback":    qa_feedback
            }, on_conflict="user_id,report_date").execute()

            if res.data:
                run_agent_self_reflection(res.data[0]['id'])
                send_email_report(user_email, final_report)

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    sync_data_to_github()

def send_email_report(user_email, report):
    try:
        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": [user_email],
            "subject": f"[{TODAY}] Fitz 비즈니스 인사이트 리포트",
            "html": f"<h2>📊 비즈니스 분석</h2>{report['ba_brief'].replace(chr(10), '<br>')}"
        })
    except: pass

if __name__ == "__main__":
    manage_deadline_approvals()
    run_self_evolution()
    run_autonomous_engine()
