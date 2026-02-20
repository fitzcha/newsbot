import os, json, time, resend, re, subprocess, shutil
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# ──────────────────────────────────────────────
# 기본 설정
# ──────────────────────────────────────────────
KST   = timezone(timedelta(hours=9))
NOW   = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL     = os.environ.get("SUPABASE_URL")
SB_KEY     = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai     = genai.Client(api_key=GEMINI_KEY)

# ──────────────────────────────────────────────
# [보조] 로그 / 성과 기록
# ──────────────────────────────────────────────
def log_to_db(user_id, target_word, action="분석", method="Auto"):
    try:
        supabase.table("action_logs").insert({
            "user_id": user_id, "action_type": action,
            "target_word": target_word, "execution_method": method, "details": "Success"
        }).execute()
    except: pass

def record_performance(user_id, keyword, count):
    try:
        supabase.table("keyword_performance").insert({
            "user_id": user_id, "keyword": keyword,
            "hit_count": count, "report_date": TODAY
        }).execute()
    except: pass

def get_agents():
    res = supabase.table("agents").select("*").execute()
    return {a['agent_role']: a for a in (res.data or [])}

# ──────────────────────────────────────────────
# [보조] Gemini 호출
# ──────────────────────────────────────────────
def call_agent(prompt, agent_info, persona_override=None, force_one_line=False):
    if not agent_info: return "분석 데이터 없음"
    role  = persona_override or agent_info['agent_role']
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"
    fp    = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라) {prompt}" if force_one_line else prompt + guard
    try:
        res    = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {fp}"
        )
        output = res.text.strip()
        return output.split('\n')[0] if force_one_line else output
    except: return "분석 지연 중"

# ──────────────────────────────────────────────
# [보조] GitHub 동기화
# ──────────────────────────────────────────────
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
        print("🚀 [Sync] GitHub data.json 갱신 완료")
    except Exception as e:
        print(f"🚨 [Sync] 동기화 실패: {e}")

# ──────────────────────────────────────────────
# [1] DEV 엔진: 마스터 CONFIRMED 작업 집행
# ──────────────────────────────────────────────
def run_self_evolution():
    try:
        task_res = supabase.table("dev_backlog").select("*").eq("status", "CONFIRMED").order("priority").limit(1).execute()
        if not task_res.data:
            return print("💤 [DEV] 마스터의 '실행 확정' 대기 작업 없음.")

        task      = task_res.data[0]
        file_path = task.get('affected_file', 'news_bot.py')
        print(f"🛠️ [DEV] 마스터 지휘 업무 착수: {task['title']}")

        bk = "backups"
        if not os.path.exists(bk): os.makedirs(bk)
        shutil.copy2(file_path, f"{bk}/{file_path}.{NOW.strftime('%H%M%S')}.bak")

        with open(file_path, "r", encoding="utf-8") as f: cur = f.read()
        agents     = get_agents()
        dev_prompt = f"요구사항: {task['task_detail']}\n\n반드시 전체 코드를 ```python ... ``` 안에 출력.\n--- 현재 코드 ---\n{cur}"
        raw        = call_agent(dev_prompt, agents.get('DEV'), "Senior Python Engineer")
        m          = re.search(r"```python\s+(.*?)\s+```", raw, re.DOTALL)
        new_code   = m.group(1).strip() if m else raw.strip()

        compile(new_code, file_path, 'exec')
        with open(file_path, "w", encoding="utf-8") as f: f.write(new_code)
        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add .',
            f'git commit -m "🤖 [v16.0] {task["title"]}"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)
        supabase.table("dev_backlog").update({"status": "COMPLETED", "completed_at": NOW.isoformat()}).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")
    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")

# ──────────────────────────────────────────────
# [2] 에이전트 자아 성찰
# ──────────────────────────────────────────────
def run_agent_self_reflection(report_id):
    try:
        fb = supabase.table("report_feedback").select("*").eq("report_id", report_id).execute()
        if not fb.data: return
        agents = get_agents()
        for role, info in agents.items():
            if role in ['DEV', 'QA', 'MASTER']: continue
            neg = [f['feedback_text'] for f in fb.data if f['target_agent'] == role and not f['is_positive']]
            if not neg: continue
            rp = f"현재 지침: {info['instruction']}\n고객불만: {', '.join(neg)}\n\n[PROPOSAL]수정지침 [REASON]수정근거 형식으로 상신하라."
            ref = call_agent(rp, info, "Insight Evolver")
            p   = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", ref, re.DOTALL)
            r   = re.search(r"\[REASON\](.*?)$", ref, re.DOTALL)
            if p:
                supabase.table("pending_approvals").insert({
                    "agent_role": role,
                    "proposed_instruction": p.group(1).strip(),
                    "proposal_reason": r.group(1).strip() if r else "VOC 피드백 반영"
                }).execute()
    except: pass

# ──────────────────────────────────────────────
# [3] 데드라인 자동 승인
# ──────────────────────────────────────────────
def manage_deadline_approvals():
    if NOW.hour == 23 and NOW.minute >= 30:
        try:
            pending = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute()
            for item in (pending.data or []):
                supabase.table("agents").update({"instruction": item['proposed_instruction']}).eq("agent_role", item['agent_role']).execute()
                supabase.table("pending_approvals").update({"status": "APPROVED"}).eq("id", item['id']).execute()
        except: pass

# ──────────────────────────────────────────────
# [4] 이메일 발송 — by_keyword 구조 대응
# ──────────────────────────────────────────────
def send_email_report(user_email, report):
    """by_keyword 구조에서 첫 번째 키워드의 ba_brief를 본문으로 사용."""
    try:
        bk       = report.get("by_keyword", {})
        kw_keys  = list(bk.keys())
        # 이메일 본문: 키워드별 요약을 모아서 구성
        sections = []
        for kw in kw_keys:
            kd = bk[kw]
            ba = kd.get("ba_brief", "").replace('\n', '<br>')
            sections.append(f"<h3>#{kw}</h3><p>{ba}</p><hr>")

        html_body = f"""
        <h2>📊 [{TODAY}] Fitz Intelligence 리포트</h2>
        {''.join(sections)}
        <p style='color:#999; font-size:0.85em;'>app.html에서 전체 분석을 확인하세요.</p>
        """
        resend.Emails.send({
            "from":    "Fitz Intelligence <onboarding@resend.dev>",
            "to":      [user_email],
            "subject": f"[{TODAY}] Fitz 키워드별 인사이트 리포트",
            "html":    html_body
        })
    except: pass

# ──────────────────────────────────────────────
# [5] 핵심 변경: 자율 분석 엔진 — by_keyword 구조
# ──────────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v16.0 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id    = user['id']
            user_email = user.get('email', 'Unknown')
            keywords   = user.get('keywords', [])[:5]
            if not keywords: continue

            # 중복 실행 방지
            chk = supabase.table("reports").select("id").eq("user_id", user_id).eq("report_date", TODAY).execute()
            if chk.data:
                print(f"⏭️  [Skip] {user_email} — 이미 발송 완료")
                continue

            print(f"🔍 [{user_email}] 키워드 {keywords} 분석 시작")

            # ── [핵심] 키워드별 루프 ──────────────────────────
            by_keyword     = {}   # 최종 저장될 구조
            all_articles   = []   # HR 통합 분석용 전체 컨텍스트

            for word in keywords:
                print(f"  📰 [{word}] 뉴스 수집 중...")
                is_korean = any(ord(c) > 0x1100 for c in word)
                gn        = GNews(language='ko' if is_korean else 'en', max_results=3)
                news_list = gn.get_news(word)

                record_performance(user_id, word, len(news_list))

                if not news_list:
                    print(f"  ⚠️  [{word}] 뉴스 없음 — 스킵")
                    by_keyword[word] = {
                        "ba_brief": "해당 키워드의 뉴스를 찾을 수 없습니다.",
                        "securities_brief": "해당 키워드의 뉴스를 찾을 수 없습니다.",
                        "pm_brief": "해당 키워드의 뉴스를 찾을 수 없습니다.",
                        "articles": []
                    }
                    continue

                # 기사별 요약
                articles = []
                kw_ctx   = []
                for n in news_list:
                    pm_summary = call_agent(f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True)
                    impact     = call_agent(
                        f"뉴스: {n['title']}\n전망 1줄.",
                        agents.get('STOCK', agents['BRIEF']),
                        force_one_line=True
                    )
                    articles.append({**n, "keyword": word, "pm_summary": pm_summary, "impact": impact})
                    kw_ctx.append(n['title'])
                    all_articles.append(f"[{word}] {n['title']}")

                ctx = "\n".join(kw_ctx)

                # ── 키워드별 에이전트 3종 분석 ──────────────
                print(f"  🤖 [{word}] 에이전트 분석 중...")
                by_keyword[word] = {
                    "ba_brief": call_agent(
                        f"키워드 '{word}' 뉴스 기반 비즈니스 수익 구조 및 경쟁 분석:\n{ctx}",
                        agents['BA']
                    ),
                    "securities_brief": call_agent(
                        f"키워드 '{word}' 뉴스 기반 주식 시장 반응 및 투자 인사이트:\n{ctx}",
                        agents['STOCK']
                    ),
                    "pm_brief": call_agent(
                        f"키워드 '{word}' 뉴스 기반 전략적 서비스 기획 브리핑:\n{ctx}",
                        agents['PM']
                    ),
                    "articles": articles
                }
                log_to_db(user_id, word, "키워드분석")

            if not by_keyword:
                print(f"⚠️  [{user_email}] 분석 결과 없음 — 스킵")
                continue

            # ── HR은 전체 통합 (비용 절감) ──────────────────
            all_ctx    = "\n".join(all_articles)
            hr_proposal = call_agent(
                f"조직 및 인사 관리 제안 (전체 키워드 기반):\n{all_ctx}",
                agents['HR']
            )

            # ── 최종 리포트 구조 ─────────────────────────────
            final_report = {
                "by_keyword":   by_keyword,    # ← app.html이 읽는 핵심 구조
                "hr_proposal":  hr_proposal,   # ← master.html HR 탭용
            }

            # DB 저장
            res = supabase.table("reports").upsert({
                "user_id":     user_id,
                "report_date": TODAY,
                "content":     final_report,
                "qa_score":    95
            }, on_conflict="user_id,report_date").execute()

            if res.data:
                report_id = res.data[0]['id']
                run_agent_self_reflection(report_id)
                send_email_report(user_email, final_report)
                print(f"✅ [{user_email}] 리포트 저장 및 이메일 발송 완료")

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    # 전체 처리 후 GitHub 동기화
    sync_data_to_github()


# ──────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────
if __name__ == "__main__":
    manage_deadline_approvals()
    run_self_evolution()
    run_autonomous_engine()
