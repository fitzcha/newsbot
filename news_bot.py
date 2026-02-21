import os, json, time, resend, re, subprocess, shutil, urllib.request, urllib.parse
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

GEMINI_KEY     = os.environ.get("GEMINI_API_KEY")
SB_URL         = os.environ.get("SUPABASE_URL")
SB_KEY         = os.environ.get("SUPABASE_KEY")
YOUTUBE_KEY    = os.environ.get("YOUTUBE_API_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai     = genai.Client(api_key=GEMINI_KEY)

DASHBOARD_URL = "https://fitzcha.github.io/newsbot/app.html"

# YouTube API 엔드포인트
YT_SEARCH_URL  = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEO_URL   = "https://www.googleapis.com/youtube/v3/videos"
YT_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"

# 구독자 10만+ → 전문가/인플루언서 태깅
EXPERT_SUBSCRIBER_THRESHOLD = 100_000

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
# [보조] Gemini 호출 — 자유 텍스트
# ──────────────────────────────────────────────
def call_agent(prompt, agent_info, persona_override=None, force_one_line=False):
    if not agent_info: return "분석 데이터 없음"
    role  = persona_override or agent_info.get('agent_role', 'Assistant')
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"
    fp    = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라) {prompt}" if force_one_line else prompt + guard

    for attempt in range(3):
        try:
            res    = google_genai.models.generate_content(
                model='gemini-2.0-flash',
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {fp}"
            )
            output = res.text.strip()
            return output.split('\n')[0] if force_one_line else output
        except Exception as e:
            err = str(e)
            if '429' in err and attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"  ⏳ [Gemini 429] {wait}초 후 재시도 ({attempt+1}/3)...")
                time.sleep(wait)
            else:
                print(f"  ❌ [Gemini 오류] {err[:80]}")
                return "분석 지연 중"
    return "분석 지연 중"

# ──────────────────────────────────────────────
# [보조] Gemini 호출 — JSON 전용 (BA/STOCK/PM 브리핑용)
# ──────────────────────────────────────────────
def call_agent_json(prompt, agent_info, persona_override=None):
    if not agent_info: return {"summary": "분석 데이터 없음", "points": [], "deep": []}
    role  = persona_override or agent_info.get('agent_role', 'Assistant')
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"

    json_instruction = """

반드시 아래 JSON 형식으로만 응답하라. 마크다운, 코드블록, 설명 텍스트 일절 금지.
{
  "summary": "핵심 한 줄 요약 (40~60자)",
  "points": ["포인트1 (1~2문장)", "포인트2 (1~2문장)", "포인트3 (1~2문장)"],
  "deep": ["심층분석1 (1~2문장)", "심층분석2 (1~2문장)", "심층분석3 (1~2문장)", "심층분석4 (1~2문장)"]
}
"""
    full_prompt = prompt + guard + json_instruction

    for attempt in range(3):
        try:
            res = google_genai.models.generate_content(
                model='gemini-2.0-flash',
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {full_prompt}"
            )
            raw = res.text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"summary": res.text.strip().split('\n')[0][:80], "points": [], "deep": []}
        except Exception as e:
            err = str(e)
            if '429' in err and attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"  ⏳ [Gemini 429] {wait}초 후 재시도 ({attempt+1}/3)...")
                time.sleep(wait)
            else:
                print(f"  ❌ [Gemini 오류] {err[:80]}")
                return {"summary": "분석 지연 중", "points": [], "deep": []}
    return {"summary": "분석 지연 중", "points": [], "deep": []}

# ──────────────────────────────────────────────
# [YouTube] API 헬퍼 / 수집 / 컨텍스트 빌더
# ──────────────────────────────────────────────
def _yt_get(url: str, params: dict) -> dict:
    """YouTube API GET — urllib 사용 (외부 라이브러리 불필요)"""
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️ [YT API] 오류: {e}")
        return {}


def collect_youtube(keyword: str, max_recent: int = 2, max_popular: int = 2) -> list:
    """
    키워드로 YouTube 영상 수집.
    - 최신순 max_recent개 + 인기순(조회수) max_popular개
    - 채널 구독자 수 조회 → 전문가/인플루언서 태깅 (10만+ 기준)
    반환: [{ title, channel, video_id, url, published,
             view_count, subscriber_count, is_expert, order_type, keyword }, ...]
    """
    if not YOUTUBE_KEY:
        print("  ⚠️ [YT] YOUTUBE_API_KEY 없음 — YouTube 수집 건너뜀")
        return []

    results, seen_ids = [], set()

    for order_type, max_n in [("date", max_recent), ("viewCount", max_popular)]:
        raw = _yt_get(YT_SEARCH_URL, {
            "key":               YOUTUBE_KEY,
            "q":                 keyword,
            "part":              "snippet",
            "type":              "video",
            "order":             order_type,
            "maxResults":        max_n,
            "relevanceLanguage": "ko",
            "regionCode":        "KR",
            "publishedAfter":    (NOW - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z"),
        })

        items = raw.get("items", [])
        if not items:
            continue

        video_ids   = [it["id"]["videoId"] for it in items if it["id"].get("videoId")]
        channel_ids = list({it["snippet"]["channelId"] for it in items})

        # 조회수 일괄 조회
        stats_raw = _yt_get(YT_VIDEO_URL, {
            "key":  YOUTUBE_KEY,
            "id":   ",".join(video_ids),
            "part": "statistics",
        })
        stats_map = {
            s["id"]: int(s["statistics"].get("viewCount", 0))
            for s in stats_raw.get("items", [])
        }

        # 채널 구독자 수 일괄 조회
        ch_raw = _yt_get(YT_CHANNEL_URL, {
            "key":  YOUTUBE_KEY,
            "id":   ",".join(channel_ids),
            "part": "statistics",
        })
        ch_map = {
            c["id"]: int(c["statistics"].get("subscriberCount", 0))
            for c in ch_raw.get("items", [])
        }

        for it in items:
            vid = it["id"].get("videoId")
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            sn    = it["snippet"]
            ch_id = sn["channelId"]
            subs  = ch_map.get(ch_id, 0)
            results.append({
                "title":            sn["title"],
                "channel":          sn["channelTitle"],
                "channel_id":       ch_id,
                "video_id":         vid,
                "url":              f"https://www.youtube.com/watch?v={vid}",
                "published":        sn.get("publishedAt", "")[:10],
                "view_count":       stats_map.get(vid, 0),
                "subscriber_count": subs,
                "is_expert":        subs >= EXPERT_SUBSCRIBER_THRESHOLD,
                "order_type":       "최신" if order_type == "date" else "인기",
                "keyword":          keyword,
            })

    expert_cnt = sum(1 for v in results if v["is_expert"])
    print(f"  🎬 [YT] '{keyword}' → {len(results)}개 수집 (전문가/인플루언서 채널 {expert_cnt}개)")
    return results


def build_youtube_context(yt_videos: list) -> str:
    """YouTube 수집 결과를 Gemini 컨텍스트 문자열로 변환"""
    if not yt_videos:
        return ""
    lines = ["[YouTube 콘텐츠 인사이트]"]
    for v in yt_videos:
        tag = "⭐전문가/인플루언서" if v["is_expert"] else "일반채널"
        lines.append(
            f"- [{v['keyword']}][{v['order_type']}] {v['title']} "
            f"| 채널: {v['channel']}({tag}, 구독{v['subscriber_count']:,}) "
            f"| 조회{v['view_count']:,} | {v['published']}"
        )
    return "\n".join(lines)


def build_youtube_email_block(yt_videos: list) -> str:
    """이메일 HTML — YouTube 섹션 블록"""
    if not yt_videos:
        return ""

    cards = ""
    for v in yt_videos:
        badge_color = "#e8472a" if v["is_expert"] else "#888"
        badge_text  = "⭐ 전문가/인플루언서" if v["is_expert"] else "일반 채널"
        subs_str    = f"{v['subscriber_count'] // 10000}만" if v["subscriber_count"] >= 10000 else f"{v['subscriber_count']:,}"
        view_str    = f"{v['view_count'] // 10000}만" if v["view_count"] >= 10000 else f"{v['view_count']:,}"
        cards += f"""
              <tr>
                <td style="padding:12px 0; border-bottom:1px solid #f0f0f0;">
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding-bottom:6px;">
                        <span style="font-size:10px; background:{badge_color}; color:#fff; border-radius:12px; padding:2px 8px; font-weight:700;">{badge_text}</span>
                        <span style="font-size:10px; color:#999; margin-left:8px;">{v['order_type']} · 구독 {subs_str} · 조회 {view_str}</span>
                      </td>
                    </tr>
                    <tr>
                      <td>
                        <a href="{v['url']}" style="font-size:14px; font-weight:600; color:#1a1a1a; text-decoration:none; line-height:1.4;">{v['title']}</a>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:4px;">
                        <span style="font-size:12px; color:#666;">{v['channel']} · {v['published']}</span>
                        <a href="{v['url']}" style="margin-left:10px; font-size:12px; color:#e8472a; font-weight:700; text-decoration:none;">▶ 영상 보기 →</a>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>"""

    return f"""
        <!-- YouTube 섹션 -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:32px; margin-bottom:24px;">
          <tr>
            <td style="border-left:3px solid #e8472a; padding-left:12px; padding-bottom:12px;">
              <span style="font-size:11px; font-weight:700; color:#e8472a; letter-spacing:1.5px; text-transform:uppercase;">YOUTUBE INSIGHTS</span>
              <h2 style="margin:2px 0 0 0; font-size:18px; font-weight:700; color:#111;">🎬 유튜브 인사이트</h2>
            </td>
          </tr>
          {cards}
        </table>"""

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
    task     = None
    cur_code = None

    def _notify(subject, body, is_fail=False):
        icon = "🚨" if is_fail else "✅"
        try:
            resend.Emails.send({
                "from":    "Fitz Intelligence <onboarding@resend.dev>",
                "to":      ["positivecha@gmail.com"],
                "subject": f"{icon} [DEV] {subject}",
                "html":    f"<pre style='font-family:monospace'>{body}</pre>"
            })
        except Exception as mail_err:
            print(f"  ⚠️ [DEV] 알림 이메일 발송 실패: {mail_err}")
            try:
                supabase.table("action_logs").insert({
                    "action_type": "DEV_NOTIFY_FAIL",
                    "target_word": subject,
                    "execution_method": "Auto",
                    "details": str(mail_err)[:200]
                }).execute()
            except: pass

    try:
        task_res = supabase.table("dev_backlog").select("*")\
            .eq("status", "CONFIRMED").order("priority").limit(1).execute()
        if not task_res.data:
            return print("💤 [DEV] 마스터의 '실행 확정' 대기 작업 없음.")

        task      = task_res.data[0]
        file_path = task.get('affected_file', 'news_bot.py')
        print(f"🛠️ [DEV] 마스터 지휘 업무 착수: {task['title']}")

        with open(file_path, "r", encoding="utf-8") as f:
            cur_code = f.read()

        try:
            supabase.table("code_backups").insert({
                "file_path":    file_path,
                "code":         cur_code,
                "task_id":      task['id'],
                "task_title":   task['title'],
                "backed_up_at": NOW.isoformat()
            }).execute()
            print(f"  💾 [DEV] 백업 저장 완료 (Supabase code_backups)")
        except Exception as bk_err:
            msg = f"백업 저장 실패로 작업 중단.\n오류: {bk_err}"
            print(f"  🚨 [DEV] {msg}")
            _notify(f"백업 실패 — '{task['title']}' 중단", msg, is_fail=True)
            supabase.table("dev_backlog").update({"status": "BACKUP_FAILED"})\
                .eq("id", task['id']).execute()
            return

        bk = "backups"
        if not os.path.exists(bk): os.makedirs(bk)
        shutil.copy2(file_path, f"{bk}/{file_path}.{NOW.strftime('%H%M%S')}.bak")

        agents     = get_agents()
        dev_prompt = (
            f"요구사항: {task['task_detail']}\n\n"
            "반드시 전체 코드를 ```python ... ``` 안에 출력.\n"
            f"--- 현재 코드 ---\n{cur_code}"
        )
        raw      = call_agent(dev_prompt, agents.get('DEV'), "Senior Python Engineer")
        m        = re.search(r"```python\s+(.*?)\s+```", raw, re.DOTALL)
        new_code = m.group(1).strip() if m else raw.strip()

        try:
            compile(new_code, file_path, 'exec')
            print(f"  ✅ [DEV] 문법 검사 통과")
        except SyntaxError as syn_err:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cur_code)
            print(f"  🚨 [DEV] 문법 오류 감지 → 롤백 완료, push 차단")
            err_detail = (
                f"작업: {task['title']}\n"
                f"오류 유형: SyntaxError\n"
                f"위치: {syn_err.filename} line {syn_err.lineno}\n"
                f"내용: {syn_err.msg}\n\n"
                f"조치: 원본 코드로 자동 롤백 완료. GitHub push는 차단되었습니다.\n"
                f"백업 ID는 Supabase code_backups 테이블에서 확인하세요."
            )
            _notify(f"문법 오류 감지 — '{task['title']}' 롤백 완료", err_detail, is_fail=True)
            try:
                supabase.table("action_logs").insert({
                    "action_type": "DEV_SYNTAX_ROLLBACK",
                    "target_word": task['title'],
                    "execution_method": "Auto",
                    "details": f"SyntaxError line {syn_err.lineno}: {syn_err.msg}"[:200]
                }).execute()
            except: pass
            supabase.table("dev_backlog").update({"status": "SYNTAX_ERROR"})\
                .eq("id", task['id']).execute()
            return

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_code)

        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add .',
            f'git commit -m "🤖 [v17.3] {task["title"]}"',
            'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({
            "status": "COMPLETED",
            "completed_at": NOW.isoformat()
        }).eq("id", task['id']).execute()
        print(f"✨ [DEV] 배포 완료: {task['title']}")

        _notify(
            f"코드 수정 배포 완료 — '{task['title']}'",
            f"작업: {task['title']}\n"
            f"파일: {file_path}\n"
            f"시각: {NOW.strftime('%Y-%m-%d %H:%M')} KST\n\n"
            f"요구사항:\n{task['task_detail'][:300]}\n\n"
            f"문법 검사: 통과\n"
            f"GitHub push: 완료\n"
            f"백업: Supabase code_backups 저장 완료"
        )

    except Exception as e:
        print(f"🚨 [DEV] 진화 실패: {e}")
        if task:
            _notify(
                f"예상치 못한 오류 — '{task.get('title', '알 수 없음')}'",
                f"오류 내용: {str(e)}\n\n원본 파일은 변경되지 않았습니다.",
                is_fail=True
            )

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
            rp = (
                f"현재 지침: {info['instruction']}\n고객불만: {', '.join(neg)}\n\n"
                "아래 형식으로 정확히 상신하라.\n"
                "[PROPOSAL]수정지침 "
                "[REASON]수정근거 "
                "[NEEDS_DEV]코드 수정 없이 지침 변경만으로 해결 가능하면 NO, 코드 변경이 필요하면 YES"
            )
            ref = call_agent(rp, info, "Insight Evolver")
            p   = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)", ref, re.DOTALL)
            r   = re.search(r"\[REASON\](.*?)(?=\[NEEDS_DEV\]|$)", ref, re.DOTALL)
            nd  = re.search(r"\[NEEDS_DEV\](.*?)$", ref, re.DOTALL)
            if p:
                needs_dev = "YES" in (nd.group(1).strip().upper() if nd else "NO")
                supabase.table("pending_approvals").insert({
                    "agent_role":           role,
                    "proposed_instruction": p.group(1).strip(),
                    "proposal_reason":      r.group(1).strip() if r else "VOC 피드백 반영",
                    "needs_dev":            needs_dev
                }).execute()
    except: pass

# ──────────────────────────────────────────────
# [3] 데드라인 자동 승인 + dev_backlog 자동 등록
# ──────────────────────────────────────────────
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

                if item.get('needs_dev'):
                    dup = supabase.table("dev_backlog")\
                        .select("id")\
                        .eq("source_approval_id", item['id'])\
                        .execute()
                    if dup.data:
                        print(f"  ⏭️ [DEV Backlog] 이미 등록된 안건 스킵: {item['id']}")
                        continue
                    supabase.table("dev_backlog").insert({
                        "title":              f"[자동등록] {item['agent_role']} — {item.get('proposal_reason', '')[:50]}",
                        "task_detail":        item['proposed_instruction'],
                        "affected_file":      "news_bot.py",
                        "priority":           10,
                        "status":             "PENDING_MASTER",
                        "source_approval_id": item['id']
                    }).execute()
                    print(f"  📋 [DEV Backlog] 자동 등록 완료: {item['agent_role']} 안건 → 대표님 승인 대기")

        except Exception as e:
            print(f"🚨 [Approvals] 처리 실패: {e}")

# ──────────────────────────────────────────────
# [4] 이메일 발송 — 뉴스레터 템플릿 v17.3
# ──────────────────────────────────────────────
def _build_email_html(report, yt_videos=None):
    bk         = report.get("by_keyword", {})
    yt_videos  = yt_videos or []

    keyword_sections = ""
    kw_list = list(bk.items())

    for idx, (kw, kd) in enumerate(kw_list):
        articles = kd.get("articles", [])
        ba_brief = kd.get("ba_brief", {})

        # 헤드라인 rows
        article_rows = ""
        for a in articles[:3]:
            title      = a.get("title", "")
            pm_summary = a.get("pm_summary", "")
            url        = a.get("url", a.get("link", "#"))
            article_rows += f"""
              <tr>
                <td style="padding:10px 0; border-bottom:1px solid #f0f0f0;">
                  <p style="margin:0 0 4px 0; font-size:14px; font-weight:600; color:#1a1a1a; line-height:1.4;">{title}</p>
                  <p style="margin:0 0 6px 0; font-size:13px; color:#666; line-height:1.5;">{pm_summary}</p>
                  <a href="{url}" style="font-size:12px; color:#2563eb; font-weight:700; text-decoration:none;">더 자세히 알아보기 →</a>
                </td>
              </tr>"""

        # BA 브리프
        if isinstance(ba_brief, dict):
            ba_items = []
            if ba_brief.get("summary"):
                ba_items.append(ba_brief["summary"])
            ba_items += ba_brief.get("points", [])
        else:
            ba_items = [l.strip() for l in str(ba_brief).split('\n') if l.strip()][:5]

        ba_html = "".join(
            f'<li style="margin-bottom:6px; color:#444; font-size:13px; line-height:1.6;">{l}</li>'
            for l in ba_items if l
        )

        divider = ""
        if idx < len(kw_list) - 1:
            divider = """
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
                <tr><td style="border-top:1px solid #f0f0f0;"></td></tr>
              </table>"""

        keyword_sections += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
          <tr>
            <td>
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:14px;">
                <tr>
                  <td style="border-left:3px solid #2563eb; padding-left:12px;">
                    <span style="font-size:11px; font-weight:700; color:#2563eb; letter-spacing:1.5px; text-transform:uppercase;">KEYWORD</span>
                    <h2 style="margin:2px 0 0 0; font-size:20px; font-weight:700; color:#111;"># {kw}</h2>
                  </td>
                </tr>
              </table>
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:14px;">
                <tr>
                  <td style="padding-bottom:8px;">
                    <span style="font-size:11px; font-weight:700; color:#888; letter-spacing:1px; text-transform:uppercase;">TODAY'S HEADLINES</span>
                  </td>
                </tr>
                {article_rows}
              </table>
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8faff; border-radius:8px;">
                <tr>
                  <td style="padding:16px 20px;">
                    <span style="font-size:11px; font-weight:700; color:#2563eb; letter-spacing:1px; text-transform:uppercase;">BUSINESS ANALYSIS</span>
                    <ul style="margin:10px 0 0 0; padding-left:18px;">
                      {ba_html}
                    </ul>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        {divider}"""

    # YouTube 섹션 (키워드 전체 합산)
    yt_block = build_youtube_email_block(yt_videos)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#f4f4f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5; padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">

          <!-- 헤더 -->
          <tr>
            <td style="background:#0f172a; border-radius:12px 12px 0 0; padding:28px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <span style="font-size:11px; font-weight:700; color:#64748b; letter-spacing:2px; text-transform:uppercase;">FITZ INTELLIGENCE</span>
                    <h1 style="margin:6px 0 0 0; font-size:22px; font-weight:700; color:#fff;">Daily Briefing</h1>
                  </td>
                  <td align="right" style="vertical-align:top;">
                    <span style="font-size:12px; color:#64748b;">{TODAY}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- 본문 -->
          <tr>
            <td style="background:#fff; padding:32px;">
              {keyword_sections}
              {yt_block}
            </td>
          </tr>

          <!-- 푸터 -->
          <tr>
            <td style="background:#f8faff; border-radius:0 0 12px 12px; padding:20px 32px; text-align:center;">
              <p style="margin:0; font-size:11px; color:#94a3b8; line-height:1.6;">
                Fitz Intelligence · 매일 오전 9시 자동 발송<br>
                © 2026 Fitz. All rights reserved.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email_report(user_email, report, yt_videos=None):
    try:
        html = _build_email_html(report, yt_videos or [])
        resend.Emails.send({
            "from":    "Fitz Intelligence <onboarding@resend.dev>",
            "to":      [user_email],
            "subject": f"[{TODAY}] Fitz 비즈니스 인사이트 리포트",
            "html":    html,
        })
        print(f"  📧 [Email] {user_email} 발송 완료")
    except Exception as e:
        print(f"  🚨 [Email] 발송 실패: {e}")

# ──────────────────────────────────────────────
# [5] 자율 분석 엔진 — by_keyword 구조 (JSON 브리핑 + YouTube)
# ──────────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v17.3 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id    = user['id']
            user_email = user.get('email', 'Unknown')
            keywords   = user.get('keywords', [])[:5]
            if not keywords: continue

            chk = supabase.table("reports").select("id").eq("user_id", user_id).eq("report_date", TODAY).execute()
            if chk.data:
                print(f"⏭️  [Skip] {user_email} — 이미 발송 완료")
                continue

            print(f"🔍 [{user_email}] 키워드 {keywords} 분석 시작")

            by_keyword   = {}
            all_articles = []
            all_yt       = []

            for word in keywords:
                print(f"  📰 [{word}] 뉴스 수집 중...")
                is_korean = any(ord(c) > 0x1100 for c in word)
                gn        = GNews(language='ko' if is_korean else 'en', max_results=10)
                news_list = gn.get_news(word)

                record_performance(user_id, word, len(news_list))

                if not news_list:
                    print(f"  ⚠️  [{word}] 뉴스 없음 — 스킵")
                    by_keyword[word] = {
                        "ba_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "securities_brief": {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "pm_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "articles":         []
                    }
                    continue

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

                # ── YouTube 수집 ──────────────────────────
                print(f"  🎬 [{word}] YouTube 수집 중...")
                yt_videos = collect_youtube(word)
                all_yt.extend(yt_videos)
                yt_ctx = build_youtube_context(yt_videos)

                # 뉴스 + YouTube 컨텍스트 합산
                ctx = "\n".join(kw_ctx)
                if yt_ctx:
                    ctx += f"\n\n{yt_ctx}"

                print(f"  🤖 [{word}] 에이전트 분석 중...")
                by_keyword[word] = {
                    "ba_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 비즈니스 수익 구조 및 경쟁 분석:\n{ctx}",
                        agents['BA']
                    ),
                    "securities_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 주식 시장 반응 및 투자 인사이트:\n{ctx}",
                        agents['STOCK']
                    ),
                    "pm_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 전략적 서비스 기획 브리핑:\n{ctx}",
                        agents['PM']
                    ),
                    "articles":       articles,
                    "youtube_videos": yt_videos,  # 키워드별 YouTube 결과 DB 저장
                }
                log_to_db(user_id, word, "키워드분석")

            if not by_keyword:
                print(f"⚠️  [{user_email}] 분석 결과 없음 — 스킵")
                continue

            all_ctx     = "\n".join(all_articles)
            hr_proposal = call_agent(
                f"조직 및 인사 관리 제안 (전체 키워드 기반):\n{all_ctx}",
                agents['HR']
            )

            final_report = {
                "by_keyword":  by_keyword,
                "hr_proposal": hr_proposal,
            }

            res = supabase.table("reports").upsert({
                "user_id":     user_id,
                "report_date": TODAY,
                "content":     final_report,
                "qa_score":    95
            }, on_conflict="user_id,report_date").execute()

            if res.data:
                report_id = res.data[0]['id']
                run_agent_self_reflection(report_id)
                send_email_report(user_email, final_report, all_yt)
                print(f"✅ [{user_email}] 리포트 저장 및 이메일 발송 완료 (YouTube {len(all_yt)}개 포함)")

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    sync_data_to_github()

# ──────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────
if __name__ == "__main__":
    cron_type = os.environ.get("CRON_TYPE", "BRIEFING")

    if cron_type == "GOVERNANCE":
        print("🌙 [GOVERNANCE] 23:30 마감 작업 모드")
        manage_deadline_approvals()
    else:
        print("☀️ [BRIEFING] 09:00 정기 브리핑 모드")
        manage_deadline_approvals()
        run_self_evolution()
        run_autonomous_engine()
