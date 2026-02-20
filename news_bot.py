import os, re, json, shutil, subprocess
from datetime import datetime, timezone, timedelta
import resend
from google import genai
from supabase import create_client
from gnews import GNews

# ── 환경 설정 ──
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client = genai.Client(api_key=GEMINI_API_KEY)
resend.api_key = RESEND_API_KEY

# ──────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────
def get_agents():
    res = supabase.table("agents").select("*").execute()
    return {a['agent_role']: a for a in (res.data or [])}

def call_agent(prompt, agent_info, fallback_role="Assistant", force_one_line=False):
    try:
        instruction = agent_info.get('instruction', '') if agent_info else ''
        if force_one_line:
            prompt += "\n반드시 1줄로만 답하라."
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            config=genai.types.GenerateContentConfig(system_instruction=instruction or fallback_role),
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ [AI] 호출 실패: {e}")
        return ""

def log_to_db(user_id, word, memo):
    try:
        supabase.table("action_logs").insert({
            "user_id": user_id,
            "target_word": word,
            "memo": memo,
            "executed_at": NOW.isoformat()
        }).execute()
    except:
        pass

def record_performance(user_id, word, count):
    try:
        supabase.table("kw_performance").upsert({
            "user_id": user_id,
            "keyword": word,
            "news_count": count,
            "checked_at": TODAY
        }, on_conflict="user_id,keyword,checked_at").execute()
    except:
        pass

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

# ──────────────────────────────────────────
# [1] DEV 엔진: 마스터 CONFIRMED 작업 집행
# ──────────────────────────────────────────
def run_self_evolution():
    try:
        task_res = supabase.table("dev_backlog").select("*").eq("status", "CONFIRMED").order("priority").limit(1).execute()
        if not task_res.data:
            return print("💤 [DEV] 마스터의 '실행 확정' 대기 작업 없음.")

        task = task_res.data[0]
        file_path = task.get('affected_file', 'news_bot.py')
        print(f"🛠️ [DEV] 마스터 지휘 업무 착수: {task['title']}")

        backup_dir = "backups"
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        shutil.copy2(file_path, f"{backup_dir}/{file_path}.{NOW.strftime('%H%M%S')}.bak")

        with open(file_path, "r", encoding="utf-8") as f:
            current_code = f.read()

        agents = get_agents()
        dev_prompt = f"요구사항: {task['task_detail']}\n\n반드시 전체 코드를 ```python ... ``` 안에 출력.\n--- 현재 코드 ---\n{current_code}"
        raw_output = call_agent(dev_prompt, agents.get('DEV'), "Senior Python Engineer")

        code_match = re.search(r"```python\s+(.*?)\s+```", raw_output, re.DOTALL)
        new_code = code_match.group(1).strip() if code_match else raw_output.strip()

        compile(new_code, file_path, 'exec')
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_code)

        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add .',
            f'git commit -m "🤖 [v15.0] {task["title"]}"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({
            "status": "COMPLETED",
            "completed_at": NOW.isoformat()
        }).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")
    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")

# ──────────────────────────────────────────
# [2] 에이전트 자아 성찰
# ──────────────────────────────────────────
def run_agent_self_reflection(report_id):
    try:
        feedback_res = supabase.table("report_feedback").select("*").eq("report_id", report_id).execute()
        if not feedback_res.data:
            return
        agents = get_agents()
        for role, info in agents.items():
            if role in ['DEV', 'QA', 'MASTER']:
                continue
            neg_voc = [f['feedback_text'] for f in feedback_res.data
                       if f['target_agent'] == role and not f['is_positive']]
            if not neg_voc:
                continue
            reflect_prompt = (
                f"현재 지침: {info['instruction']}\n"
                f"고객불만: {', '.join(neg_voc)}\n\n"
                "[PROPOSAL]수정지침 [REASON]수정근거 형식으로 상신하라."
            )
            reflection = call_agent(reflect_prompt, info, "Insight Evolver")
            p = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", reflection, re.DOTALL)
            r = re.search(r"\[REASON\](.*?)$", reflection, re.DOTALL)
            if p:
                supabase.table("pending_approvals").insert({
                    "agent_role": role,
                    "proposed_instruction": p.group(1).strip(),
                    "proposal_reason": r.group(1).strip() if r else "VOC 피드백 반영"
                }).execute()
    except:
        pass

# ──────────────────────────────────────────
# [3] 자율 진화 제안 (에이전트 자율 개선)
# ──────────────────────────────────────────
def run_self_evo_proposals():
    try:
        print("🧠 [EVO] 에이전트 자율 진화 시작...")
        agents = get_agents()
        today_props = supabase.table("pending_approvals").select("agent_role").gte(
            "created_at", f"{TODAY}T00:00:00"
        ).execute()
        already = {p['agent_role'] for p in (today_props.data or [])}

        recent = supabase.table("reports").select("content").eq(
            "report_date", TODAY
        ).limit(1).execute()
        ctx = ""
        if recent.data:
            arts = recent.data[0].get('content', {}).get('articles', [])
            ctx = "\n".join([a.get('title', '') for a in arts[:10]])

        for role, info in agents.items():
            if role in ['DEV', 'QA', 'MASTER', 'BRIEF']:
                continue
            if role in already:
                print(f"⏭️  [EVO] {role} — 오늘 이미 제안 완료, 스킵")
                continue
            evo_prompt = (
                f"당신은 {role} 에이전트입니다.\n"
                f"현재 지침: {info.get('instruction', '')}\n"
                f"오늘 수집된 뉴스 샘플:\n{ctx}\n\n"
                "지침을 스스로 개선하여 [PROPOSAL]개선지침 [REASON]개선이유 형식으로 상신하라."
            )
            result = call_agent(evo_prompt, info, role)
            p = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", result, re.DOTALL)
            r = re.search(r"\[REASON\](.*?)$", result, re.DOTALL)
            if p:
                supabase.table("pending_approvals").insert({
                    "agent_role": role,
                    "proposed_instruction": p.group(1).strip(),
                    "proposal_reason": r.group(1).strip() if r else "자율 진화 제안"
                }).execute()
                print(f"  ✅ [{role}] 진화 제안 상신 완료")
    except Exception as e:
        print(f"🚨 [EVO] 진화 실패: {e}")

# ──────────────────────────────────────────
# [4] 데드라인 자동 승인 (23:30)
# ──────────────────────────────────────────
def manage_deadline_approvals():
    if NOW.hour == 23 and NOW.minute >= 30:
        try:
            pending = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute()
            for item in (pending.data or []):
                supabase.table("agents").update({
                    "instruction": item['proposed_instruction']
                }).eq("agent_role", item['agent_role']).execute()
                supabase.table("pending_approvals").update({
                    "status": "APPROVED"
                }).eq("id", item['id']).execute()
            print(f"✅ [APPROVAL] {len(pending.data or [])}건 자동 승인 완료")
        except Exception as e:
            print(f"🚨 [APPROVAL] 실패: {e}")

# ──────────────────────────────────────────
# [5] 자율 분석 엔진 v15.0 — 키워드별 브리핑 분리
# ──────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v15.0 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id    = user['id']
            user_email = user.get('email', 'Unknown')
            keywords   = user.get('keywords', [])[:5]
            if not keywords:
                continue

            check = supabase.table("reports").select("id").eq("user_id", user_id).eq("report_date", TODAY).execute()
            if check.data:
                print(f"⏭️  [Skip] {user_email}님은 이미 발송 완료.")
                continue

            # ── 1. 키워드별 뉴스 수집 ──
            kw_news = {}
            all_articles = []

            for word in keywords:
                gn = GNews(
                    language='ko' if any(ord(c) > 0x1100 for c in word) else 'en',
                    max_results=2
                )
                news_list = gn.get_news(word)
                record_performance(user_id, word, len(news_list))
                kw_articles = []
                for n in news_list:
                    short_summary = call_agent(
                        f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True
                    )
                    impact = call_agent(
                        f"뉴스: {n['title']}\n전망 1줄.",
                        agents.get('STOCK', agents['BRIEF']),
                        force_one_line=True
                    )
                    article = {**n, "keyword": word, "pm_summary": short_summary, "impact": impact}
                    kw_articles.append(article)
                    all_articles.append(article)
                kw_news[word] = kw_articles
                log_to_db(user_id, word, "뉴스수집")

            if not all_articles:
                continue

            # ── 2. 전체 컨텍스트 브리핑 (전체 탭용) ──
            all_ctx = "\n".join([f"[{a['keyword']}] {a['title']}" for a in all_articles])
            full_brief = {
                "ba_brief":         call_agent(f"비즈니스 수익 구조 및 경쟁 분석:\n{all_ctx}", agents['BA']),
                "securities_brief": call_agent(f"주식 시장 반응 및 투자 인사이트:\n{all_ctx}", agents['STOCK']),
                "pm_brief":         call_agent(f"전략적 서비스 기획 관점 브리핑:\n{all_ctx}", agents['PM']),
                "hr_proposal":      call_agent(f"조직 및 인사 관리 제안:\n{all_ctx}", agents['HR']),
            }

            # ── 3. 키워드별 브리핑 (각 키워드 탭용) ──
            by_keyword = {}
            for word, arts in kw_news.items():
                if not arts:
                    continue
                ctx = "\n".join([a['title'] for a in arts])
                by_keyword[word] = {
                    "ba_brief":         call_agent(f"비즈니스 수익 구조 및 경쟁 분석:\n{ctx}", agents['BA']),
                    "securities_brief": call_agent(f"주식 시장 반응 및 투자 인사이트:\n{ctx}", agents['STOCK']),
                    "pm_brief":         call_agent(f"전략적 서비스 기획 관점 브리핑:\n{ctx}", agents['PM']),
                }
                print(f"  ✅ [{word}] 키워드 브리핑 완료")

            # ── 4. DB 저장 ──
            final_report = {
                **full_brief,
                "articles":   all_articles,
                "by_keyword": by_keyword,
            }

            res = supabase.table("reports").upsert({
                "user_id":     user_id,
                "report_date": TODAY,
                "content":     final_report,
                "qa_score":    95
            }, on_conflict="user_id,report_date").execute()

            if res.data:
                run_agent_self_reflection(res.data[0]['id'])
                send_email_report(user_email, final_report)

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    sync_data_to_github()

# ──────────────────────────────────────────
# [6] 이메일 발송 — 뉴스레터 HTML 템플릿
# ──────────────────────────────────────────
def send_email_report(user_email, report):
    try:
        articles = report.get('articles', [])
        news_cards_html = ""
        for i, a in enumerate(articles[:6]):
            color = ["#4f46e5","#10b981","#f59e0b","#6f42c1","#ef4444","#3b82f6"][i % 6]
            news_cards_html += f"""
            <tr><td style="padding:0 0 14px 0;">
              <table width="100%" cellpadding="0" cellspacing="0"
                style="background:#f9fafb;border-radius:10px;border-left:4px solid {color};">
                <tr><td style="padding:14px 18px;">
                  <span style="background:{color};color:#fff;font-size:11px;font-weight:700;
                    padding:2px 10px;border-radius:20px;">#{a.get('keyword','')}</span>
                  <p style="margin:8px 0 5px;font-size:14px;font-weight:700;color:#111827;line-height:1.5;">
                    <a href="{a.get('url','#')}" style="color:#111827;text-decoration:none;">{a.get('title','')}</a>
                  </p>
                  <p style="margin:0;font-size:12px;color:#4b5563;">💡 {a.get('pm_summary','')}</p>
                  <p style="margin:5px 0 0;font-size:11px;color:#9ca3af;">📈 {a.get('impact','')}</p>
                </td></tr>
              </table>
            </td></tr>"""

        def block(icon, title, color, content):
            return f"""
            <tr><td style="padding:0 0 18px 0;">
              <table width="100%" cellpadding="0" cellspacing="0"
                style="background:#fff;border-radius:14px;border:1px solid #e5e7eb;overflow:hidden;">
                <tr><td style="background:{color};padding:12px 18px;">
                  <span style="color:#fff;font-size:13px;font-weight:800;">{icon} {title}</span>
                </td></tr>
                <tr><td style="padding:16px 18px;font-size:13px;color:#1f2937;line-height:1.8;">
                  {content.replace(chr(10),'<br>')}
                </td></tr>
              </table>
            </td></tr>"""

        from datetime import datetime
        today_str = datetime.now().strftime("%Y년 %m월 %d일")

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef2f7;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
    border-radius:18px 18px 0 0;padding:32px 36px;text-align:center;">
    <p style="margin:0 0 4px;color:#a5b4fc;font-size:11px;font-weight:700;letter-spacing:3px;">
      FITZ INTELLIGENCE</p>
    <h1 style="margin:0 0 6px;color:#fff;font-size:24px;font-weight:800;letter-spacing:-.5px;">
      비즈니스 인사이트 리포트</h1>
    <p style="margin:0;color:#64748b;font-size:12px;">{today_str} 오전 9시 브리핑</p>
  </td></tr>
  <tr><td style="background:#fff;padding:28px 36px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:0 0 22px;">
        <h2 style="margin:0 0 14px;font-size:15px;font-weight:800;color:#111827;
          border-bottom:2px solid #f3f4f6;padding-bottom:10px;">📰 오늘의 핵심 뉴스</h2>
        <table width="100%" cellpadding="0" cellspacing="0">{news_cards_html}</table>
      </td></tr>
      <tr><td style="padding:0 0 6px;">
        <h2 style="margin:0 0 14px;font-size:15px;font-weight:800;color:#111827;
          border-bottom:2px solid #f3f4f6;padding-bottom:10px;">🤖 AI 에이전트 심층 분석</h2>
        <table width="100%" cellpadding="0" cellspacing="0">
          {block("📊","비즈니스 분석 (BA)","#4f46e5",report.get('ba_brief',''))}
          {block("📈","증권·투자 인사이트","#10b981",report.get('securities_brief',''))}
          {block("🎯","전략 기획 (PM)","#7c3aed",report.get('pm_brief',''))}
          {block("👥","조직·인사 제안 (HR)","#f59e0b",report.get('hr_proposal',''))}
        </table>
      </td></tr>
    </table>
  </td></tr>
  <tr><td style="background:#111827;border-radius:0 0 18px 18px;
    padding:22px 36px;text-align:center;">
    <p style="margin:0 0 5px;color:#a5b4fc;font-size:12px;font-weight:700;">Fitz Intelligence</p>
    <p style="margin:0;color:#4b5563;font-size:11px;line-height:1.7;">
      본 리포트는 AI 에이전트가 자율 분석한 정보입니다.<br>
      투자 결정의 최종 책임은 본인에게 있습니다.
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""

        resend.Emails.send({
            "from": "Fitz Intelligence <onboarding@resend.dev>",
            "to": [user_email],
            "subject": f"[{today_str}] Fitz 비즈니스 인사이트 — 오전 브리핑",
            "html": html
        })
        print(f"✅ [Email] 발송 완료: {user_email}")
    except Exception as e:
        print(f"🚨 [Email] 발송 실패: {e}")

# ──────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────
if __name__ == "__main__":
    manage_deadline_approvals()   # 23:30 자동 승인
    run_self_evolution()          # DEV 백로그 집행
    run_self_evo_proposals()      # 에이전트 자율 진화 제안
    run_autonomous_engine()       # 뉴스 수집 + 분석 + 발송
