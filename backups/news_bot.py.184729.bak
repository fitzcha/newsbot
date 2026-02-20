import os, json, time, traceback, random, resend, re, subprocess, shutil
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# [v12.7] DB-GitHub 동기화 엔진 + 9AM KST 최적화 + 데이터 정합성 보장
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
# [New] GitHub 저장소 동기화 (data.json 강제 갱신)
# ---------------------------------------------------------
def sync_data_to_github():
    """[v12.7 추가] DB의 최신 리포트를 data.json에 쓰고 Git Push 수행"""
    try:
        print("📁 [Sync] GitHub 저장소 동기화 시작...")
        # 1. 오늘 날짜의 모든 리포트 DB에서 가져오기
        res = supabase.table("reports").select("*").eq("report_date", TODAY).execute()
        
        # 2. data.json 파일 작성
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(res.data, f, ensure_ascii=False, indent=2)
            
        # 3. Git Push 실행 (브랜드 홈에서 최신 데이터를 식별할 수 있게 함)
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
            f'git commit -m "🤖 [v12.7] {task["title"]}"', 
            'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({"status": "COMPLETED", "completed_at": NOW.isoformat()}).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")
    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")

# ---------------------------------------------------------
# [2] 에이전트 자아 성찰 및 [3] 데드라인 승인 (기존 로직 유지)
# ---------------------------------------------------------
def run_agent_self_reflection(report_id):
    try:
        feedback_res = supabase.table("report_feedback").select("*").eq("report_id", report_id).execute()
        if not feedback_res.data: return
        agents = get_agents()
        for role, info in agents.items():
            if role in ['DEV', 'QA', 'MASTER']: continue
            neg_voc = [f['feedback_text'] for f in feedback_res.data if f['target_agent'] == role and not f['is_positive']]
            if not neg_voc: continue
            reflect_prompt = f"현재 지침: {info['instruction']}\n고객불만: {', '.join(neg_voc)}\n\n[PROPOSAL]수정지침 [REASON]수정근거 형식으로 상신하라."
            reflection = call_agent(reflect_prompt, info, "Insight Evolver")
            p = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", reflection, re.DOTALL)
            r = re.search(r"\[REASON\](.*?)$", reflection, re.DOTALL)
            if p:
                supabase.table("pending_approvals").insert({
                    "agent_role": role, "proposed_instruction": p.group(1).strip(), "proposal_reason": r.group(1).strip() if r else "VOC 피드백 반영"
                }).execute()
    except: pass

def manage_deadline_approvals():
    if NOW.hour == 23 and NOW.minute >= 30:
        try:
            pending = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute()
            for item in (pending.data or []):
                supabase.table("agents").update({"instruction": item['proposed_instruction']}).eq("agent_role", item['agent_role']).execute()
                supabase.table("pending_approvals").update({"status": "APPROVED"}).eq("id", item['id']).execute()
        except: pass

# ---------------------------------------------------------
# [4] 자율 분석 엔진 (동기화 로직 통합)
# ---------------------------------------------------------
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v12.7 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id, user_email, keywords = user['id'], user.get('email', 'Unknown'), user.get('keywords', [])[:5]
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
                    impact = call_agent(f"뉴스: {n['title']}\n전망 1줄.", agents.get('STOCK', agents['BRIEF']), force_one_line=True)
                    articles_with_summary.append({**n, "keyword": word, "pm_summary": short_summary, "impact": impact})
                    all_news_context.append(f"[{word}] {n['title']}")
                log_to_db(user_id, word, "뉴스수집")

            if not articles_with_summary: continue
            ctx = "\n".join(all_news_context)
            final_report = {
                "ba_brief": call_agent(f"비즈니스 수익 구조 및 경쟁 분석:\n{ctx}", agents['BA']),
                "securities_brief": call_agent(f"주식 시장 반응 및 투자 인사이트:\n{ctx}", agents['STOCK']),
                "pm_brief": call_agent(f"전략적 서비스 기획 관점 브리핑:\n{ctx}", agents['PM']),
                "hr_proposal": call_agent(f"조직 및 인사 관리 제안:\n{ctx}", agents['HR']),
                "articles": articles_with_summary
            }

            res = supabase.table("reports").upsert({
                "user_id": user_id, "report_date": TODAY, "content": final_report, "qa_score": 95
            }, on_conflict="user_id,report_date").execute()
            
            if res.data: 
                run_agent_self_reflection(res.data[0]['id'])
                send_email_report(user_email, final_report)

        except Exception as e: 
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue
    
    # [핵심] 모든 유저 처리 후(혹은 Skip 후) 최종적으로 data.json을 갱신하여 깃허브와 동기화
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
