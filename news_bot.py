import os, json, re, subprocess, shutil, resend
from google import genai
from gnews import GNews
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
SB_URL     = os.environ.get("SUPABASE_URL")
SB_KEY     = os.environ.get("SUPABASE_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai     = genai.Client(api_key=GEMINI_KEY)

# ─────────────────────────────────────────────
# 보조 유틸
# ─────────────────────────────────────────────
def log_to_db(user_id, target_word, action="분석", method="Auto"):
    try:
        supabase.table("action_logs").insert({
            "user_id": user_id,
            "action_type": action,
            "target_word": target_word,
            "execution_method": method,
            "details": "Success"
        }).execute()
    except:
        pass

def record_performance(user_id, keyword, count):
    try:
        supabase.table("keyword_performance").insert({
            "user_id": user_id,
            "keyword": keyword,
            "hit_count": count,
            "report_date": TODAY
        }).execute()
    except:
        pass

def get_agents():
    res = supabase.table("agents").select("*").execute()
    return {a['agent_role']: a for a in (res.data or [])}

def call_agent(prompt, agent_info, persona_override=None, force_one_line=False):
    if not agent_info:
        return "분석 데이터 없음"
    role = persona_override if persona_override else agent_info.get('agent_role', 'Agent')
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"
    final_prompt = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라) {prompt}" if force_one_line else prompt + guard
    try:
        res = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"당신은 {role}입니다.\n지침: {agent_info.get('instruction','')}\n\n입력: {final_prompt}"
        )
        output = res.text.strip()
        return output.split('\n')[0] if force_one_line else output
    except:
        return "분석 지연 중"

# ─────────────────────────────────────────────
# GitHub 저장소 동기화
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# [1] DEV 엔진: 마스터 CONFIRMED 작업 집행
# ─────────────────────────────────────────────
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
            'git commit -m "🤖 [DEV] ' + task.get('title', 'update') + '"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({
            "status": "DEPLOYED",
            "completed_at": NOW.isoformat()
        }).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")

    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")

# ─────────────────────────────────────────────
# [2] 에이전트 자율 진화 제안 (v14.0 — 피드백 없어도 매일 자율 제안)
# ─────────────────────────────────────────────
def run_agent_self_reflection():
    """
    매일 9시 실행 시 각 에이전트가 자신의 현재 지침을 스스로 검토하고
    개선안을 pending_approvals에 제안합니다. 피드백이 없어도 실행됩니다.
    """
    print("🧠 [EVO] 에이전트 자율 진화 시작...")
    try:
        agents = get_agents()
        # DEV, QA, MASTER는 자율 제안 제외
        target_roles = [r for r in agents if r not in ['DEV', 'QA', 'MASTER']]

        # 오늘 이미 제안한 에이전트는 중복 제안 방지
        already_res = supabase.table("pending_approvals") \
            .select("agent_role") \
            .gte("created_at", TODAY + "T00:00:00") \
            .execute()
        already_proposed = {r['agent_role'] for r in (already_res.data or [])}

        # 오늘 수집된 뉴스 헤드라인 컨텍스트 수집
        news_ctx = ""
        try:
            report_res = supabase.table("reports") \
                .select("content") \
                .eq("report_date", TODAY) \
                .limit(1).execute()
            if report_res.data:
                articles = report_res.data[0].get('content', {}).get('articles', [])
                headlines = [a.get('title', '') for a in articles[:5]]
                news_ctx = "\n".join(headlines)
        except:
            news_ctx = "뉴스 컨텍스트 없음"

        for role in target_roles:
            if role in already_proposed:
                print(f"⏭️  [EVO] {role} — 오늘 이미 제안 완료, 스킵")
                continue

            info = agents[role]
            current_instruction = info.get('instruction', '지침 없음')

            reflect_prompt = f"""당신은 {role} 에이전트입니다.

[현재 지침]
{current_instruction}

[오늘의 주요 뉴스 헤드라인]
{news_ctx if news_ctx else '없음'}

위 정보를 바탕으로 당신의 역할을 더 잘 수행하기 위한 지침 개선안을 제안하십시오.

반드시 아래 형식으로만 답하십시오:
[PROPOSAL] 개선된 지침 전문 (현재 지침을 발전시킨 완성형으로 작성)
[REASON] 개선 이유 (1-2문장)"""

            try:
                proposal_raw = call_agent(reflect_prompt, info, f"{role} Self-Reflection")

                p = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", proposal_raw, re.DOTALL)
                r = re.search(r"\[REASON\](.*?)$", proposal_raw, re.DOTALL)

                if not p:
                    print(f"⚠️  [EVO] {role} — 형식 불일치, 스킵")
                    continue

                proposed = p.group(1).strip()
                reason   = r.group(1).strip() if r else "자율 개선 제안"

                # 현재 지침과 동일하면 스킵
                if proposed == current_instruction:
                    print(f"⏭️  [EVO] {role} — 변경사항 없음, 스킵")
                    continue

                supabase.table("pending_approvals").insert({
                    "agent_role": role,
                    "proposed_instruction": proposed,
                    "proposal_reason": reason,
                    "status": "PENDING"
                }).execute()
                print(f"✅ [EVO] {role} — 개선안 제안 완료")

            except Exception as e:
                print(f"❌ [EVO] {role} 제안 실패: {e}")
                continue

    except Exception as e:
        print(f"🚨 [EVO] 자율 진화 전체 실패: {e}")

# ─────────────────────────────────────────────
# [3] 23:30 자동 승인 (GitHub Actions 14:30 UTC 스케줄)
# ─────────────────────────────────────────────
def manage_deadline_approvals():
    if NOW.hour == 23 and NOW.minute >= 30:
        print("⏰ [AUTO] 23:30 자동 승인 실행 중...")
        try:
            pending = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute()
            for item in (pending.data or []):
                supabase.table("agents").update({
                    "instruction": item['proposed_instruction']
                }).eq("agent_role", item['agent_role']).execute()
                supabase.table("pending_approvals").update({
                    "status": "APPROVED"
                }).eq("id", item['id']).execute()
                print(f"✅ [AUTO] {item['agent_role']} 자동 승인 완료")
        except Exception as e:
            print(f"🚨 [AUTO] 자동 승인 실패: {e}")

# ─────────────────────────────────────────────
# [4] 자율 분석 엔진 (메인 리포트 생성 + 이메일 발송)
# ─────────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v14.0 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id    = user['id']
            user_email = user.get('email', 'Unknown')
            keywords   = user.get('keywords', [])[:5]
            if not keywords:
                continue

            check_report = supabase.table("reports").select("id") \
                .eq("user_id", user_id).eq("report_date", TODAY).execute()
            if check_report.data:
                print(f"⏭️  [Skip] {user_email}님은 이미 발송 완료되었습니다.")
                continue

            all_news_context, articles_with_summary = [], []
            for word in keywords:
                gn = GNews(
                    language='ko' if any(ord(c) > 0x1100 for c in word) else 'en',
                    max_results=2
                )
                news_list = gn.get_news(word)
                record_performance(user_id, word, len(news_list))
                for n in news_list:
                    short_summary = call_agent(f"뉴스: {n['title']}", agents.get('BRIEF', {}), force_one_line=True)
                    impact        = call_agent(f"뉴스: {n['title']}\n전망 1줄.", agents.get('STOCK', agents.get('BRIEF', {})), force_one_line=True)
                    articles_with_summary.append({**n, "keyword": word, "pm_summary": short_summary, "impact": impact})
                    all_news_context.append(f"[{word}] {n['title']}")
                log_to_db(user_id, word, "뉴스수집")

            if not articles_with_summary:
                continue

            ctx = "\n".join(all_news_context)
            final_report = {
                "ba_brief":        call_agent(f"비즈니스 수익 구조 및 경쟁 분석:\n{ctx}", agents.get('BA', {})),
                "securities_brief":call_agent(f"주식 시장 반응 및 투자 인사이트:\n{ctx}", agents.get('STOCK', {})),
                "pm_brief":        call_agent(f"전략적 서비스 기획 관점 브리핑:\n{ctx}", agents.get('PM', {})),
                "hr_proposal":     call_agent(f"조직 및 인사 관리 제안:\n{ctx}", agents.get('HR', {})),
                "articles":        articles_with_summary
            }

            res = supabase.table("reports").upsert({
                "user_id":     user_id,
                "report_date": TODAY,
                "content":     final_report,
                "qa_score":    95
            }, on_conflict="user_id,report_date").execute()

            if res.data:
                send_email_report(user_email, final_report)

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    sync_data_to_github()

def send_email_report(user_email, report):
    try:
        resend.Emails.send({
            "from":    "Fitz Intelligence <onboarding@resend.dev>",
            "to":      [user_email],
            "subject": f"[{TODAY}] Fitz 비즈니스 인사이트 리포트",
            "html":    f"<h2>📊 비즈니스 분석</h2>{report['ba_brief'].replace(chr(10), '<br>')}"
        })
        print(f"📧 [MAIL] {user_email} 발송 완료")
    except Exception as e:
        print(f"❌ [MAIL] 발송 실패 ({user_email}): {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    manage_deadline_approvals()   # 23:30이면 자동 승인
    run_self_evolution()          # CONFIRMED 개발 안건 배포
    run_agent_self_reflection()   # 에이전트 자율 진화 제안 (매일 실행)
    run_autonomous_engine()       # 리포트 생성 + 이메일 발송
def send_email_report(user_email, report):
    """뉴스레터 수준 HTML 이메일 발송"""
    try:
        articles = report.get('articles', [])
        
        # 뉴스 카드 HTML 생성
        news_cards_html = ""
        for i, a in enumerate(articles[:6]):  # 최대 6개
            keyword_color = ["#007bff","#28a745","#fd7e14","#6f42c1","#20c997","#dc3545"][i % 6]
            news_cards_html += f"""
            <tr>
              <td style="padding:0 0 16px 0;">
                <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fa; border-radius:12px; border-left:4px solid {keyword_color};">
                  <tr>
                    <td style="padding:16px 20px;">
                      <span style="background:{keyword_color}; color:#fff; font-size:11px; font-weight:700; padding:3px 10px; border-radius:20px; letter-spacing:0.5px;">#{a.get('keyword','')}</span>
                      <p style="margin:8px 0 6px 0; font-size:14px; font-weight:700; color:#1a1a2e; line-height:1.5;">
                        <a href="{a.get('url','#')}" style="color:#1a1a2e; text-decoration:none;">{a.get('title','')}</a>
                      </p>
                      <p style="margin:0; font-size:13px; color:#555; line-height:1.6;">💡 {a.get('pm_summary','')}</p>
                      <p style="margin:6px 0 0 0; font-size:12px; color:#888;">📈 {a.get('impact','')}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""

        # 분석 섹션 HTML 생성
        def analysis_block(icon, title, color, content):
            return f"""
            <tr>
              <td style="padding:0 0 20px 0;">
                <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff; border-radius:16px; border:1px solid #e8ecf0; overflow:hidden;">
                  <tr>
                    <td style="background:{color}; padding:14px 20px;">
                      <span style="color:#fff; font-size:14px; font-weight:800;">{icon} {title}</span>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:18px 20px; font-size:13px; color:#333; line-height:1.8;">
                      {content.replace(chr(10), '<br>')}
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""

        from datetime import datetime
        today_str = datetime.now().strftime("%Y년 %m월 %d일")

        html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#eef2f7; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7; padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">

        <!-- HEADER -->
        <tr>
          <td style="background:linear-gradient(135deg,#0f0c29,#302b63,#24243e); border-radius:20px 20px 0 0; padding:36px 40px; text-align:center;">
            <p style="margin:0 0 4px 0; color:#a78bfa; font-size:11px; font-weight:700; letter-spacing:3px; text-transform:uppercase;">Fitz Intelligence</p>
            <h1 style="margin:0 0 6px 0; color:#fff; font-size:26px; font-weight:800; letter-spacing:-0.5px;">비즈니스 인사이트 리포트</h1>
            <p style="margin:0; color:#94a3b8; font-size:13px;">{today_str} 오전 9시 브리핑</p>
          </td>
        </tr>

        <!-- BODY -->
        <tr>
          <td style="background:#fff; padding:32px 40px;">
            <table width="100%" cellpadding="0" cellspacing="0">

              <!-- 오늘의 뉴스 -->
              <tr><td style="padding:0 0 24px 0;">
                <h2 style="margin:0 0 16px 0; font-size:16px; font-weight:800; color:#1a1a2e; border-bottom:2px solid #eef2f7; padding-bottom:12px;">📰 오늘의 핵심 뉴스</h2>
                <table width="100%" cellpadding="0" cellspacing="0">
                  {news_cards_html}
                </table>
              </td></tr>

              <!-- 에이전트 분석 -->
              <tr><td style="padding:0 0 8px 0;">
                <h2 style="margin:0 0 16px 0; font-size:16px; font-weight:800; color:#1a1a2e; border-bottom:2px solid #eef2f7; padding-bottom:12px;">🤖 AI 에이전트 심층 분석</h2>
                <table width="100%" cellpadding="0" cellspacing="0">
                  {analysis_block("📊","비즈니스 분석 (BA)","#007bff", report.get('ba_brief',''))}
                  {analysis_block("📈","증권·투자 인사이트","#28a745", report.get('securities_brief',''))}
                  {analysis_block("🎯","전략 기획 (PM)","#6f42c1", report.get('pm_brief',''))}
                  {analysis_block("👥","조직·인사 제안 (HR)","#fd7e14", report.get('hr_proposal',''))}
                </table>
              </td></tr>

            </table>
          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td style="background:#1a1a2e; border-radius:0 0 20px 20px; padding:24px 40px; text-align:center;">
            <p style="margin:0 0 6px 0; color:#a78bfa; font-size:13px; font-weight:700;">Fitz Intelligence</p>
            <p style="margin:0; color:#64748b; font-size:11px; line-height:1.7;">
              본 리포트는 AI 에이전트가 자율 분석한 정보입니다.<br>투자 결정의 최종 책임은 본인에게 있습니다.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>

</body>
</html>"""

        resend.Emails.send({
            "from": "Fitz Intelligence <report@yourdomain.com>",  # 도메인 연결 후 변경
            "to": [user_email],
            "subject": f"[{today_str}] Fitz 비즈니스 인사이트 — 오전 브리핑",
            "html": html
        })
        print(f"✅ [Email] 발송 완료: {user_email}")
    except Exception as e:
        print(f"🚨 [Email] 발송 실패: {e}")
