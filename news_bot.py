import os, json, time, re, subprocess, shutil, urllib.request, urllib.parse
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

GEMINI_KEY  = os.environ.get("GEMINI_API_KEY")
SB_URL      = os.environ.get("SUPABASE_URL")
SB_KEY      = os.environ.get("SUPABASE_KEY")
YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY")

GMAIL_USER = "fitzintelligence@gmail.com"
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")
CURRENT_BACKLOG_ID = (os.environ.get("CURRENT_BACKLOG_ID") or "").strip()

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai     = genai.Client(api_key=GEMINI_KEY)

DASHBOARD_URL = "https://newsbot-smoky.vercel.app/app.html"

YT_SEARCH_URL  = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEO_URL   = "https://www.googleapis.com/youtube/v3/videos"
YT_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"

EXPERT_SUBSCRIBER_THRESHOLD = 100_000

# Gemini 토큰 단가 (USD / 1K tokens)
_GEMINI_PRICE = {
    "gemini-1.5-flash": {"input": 0.000075, "output": 0.000300},
    "gemini-1.5-pro":   {"input": 0.001250, "output": 0.005000},
    "gemini-2.0-flash": {"input": 0.000100, "output": 0.000400},
}
_DEFAULT_MODEL = "gemini-2.0-flash"

_MONITOR_TABLES = [
    "action_logs", "reports", "cost_log", "keyword_performance",
    "pending_approvals", "dev_backlog", "agents",
]

# ──────────────────────────────────────────────
# 환경변수 체크
# ──────────────────────────────────────────────
def _check_env():
    missing = []
    for key, val in [
        ("GEMINI_API_KEY",     GEMINI_KEY),
        ("SUPABASE_URL",       SB_URL),
        ("SUPABASE_KEY",       SB_KEY),
        ("GMAIL_APP_PASSWORD", GMAIL_PASS),
        ("YOUTUBE_API_KEY",    YOUTUBE_KEY),
    ]:
        if not val:
            missing.append(key)
    if missing:
        print(f"🚨 [ENV] 필수 환경변수 누락: {', '.join(missing)}")
        print("🚨 [ENV] 이메일 발송 및 일부 기능이 작동하지 않을 수 있습니다.")
    else:
        print("✅ [ENV] 환경변수 전체 확인 완료")

_check_env()

# ──────────────────────────────────────────────
# Gmail SMTP
# ──────────────────────────────────────────────
def _send_gmail(to, subject: str, html: str):
    recipients = [to] if isinstance(to, str) else to
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Fitz Intelligence <{GMAIL_USER}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, recipients, msg.as_string())

# ──────────────────────────────────────────────
# 로그 / 성과 / 비용 기록
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

def record_cost(call_type: str, input_tokens: int, output_tokens: int,
                model: str = _DEFAULT_MODEL, count: int = 1):
    """Gemini 호출 1회의 토큰/비용을 cost_log에 기록"""
    try:
        price = _GEMINI_PRICE.get(model, _GEMINI_PRICE[_DEFAULT_MODEL])
        cost  = (input_tokens / 1000 * price["input"]
                 + output_tokens / 1000 * price["output"]) * count
        supabase.table("cost_log").insert({
            "log_date":      TODAY,
            "call_type":     call_type,
            "model":         model,
            "call_count":    count,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      round(cost, 6),
        }).execute()
    except Exception as e:
        print(f"  ⚠️ [Cost] 기록 실패: {e}")

def record_supabase_stats():
    """하루 1회 — 테이블별 row 수를 supabase_stats에 스냅샷 저장"""
    try:
        counts = {}
        total  = 0
        for tbl in _MONITOR_TABLES:
            try:
                res = supabase.table(tbl).select("id", count="exact").limit(1).execute()
                n   = res.count or 0
                counts[tbl] = n
                total += n
            except:
                counts[tbl] = -1
        supabase.table("supabase_stats").upsert({
            "stat_date":  TODAY,
            "row_counts": counts,
            "total_rows": total,
            "updated_at": NOW.isoformat(),
        }, on_conflict="stat_date").execute()
        print(f"📊 [Stats] Supabase row 스냅샷 저장 완료 (total={total:,})")
    except Exception as e:
        print(f"  ⚠️ [Stats] 스냅샷 저장 실패: {e}")

def get_agents():
    res = supabase.table("agents").select("*").execute()
    return {a['agent_role']: a for a in (res.data or [])}

# ──────────────────────────────────────────────
# Gemini 호출 — 자유 텍스트
# ──────────────────────────────────────────────
def call_agent(prompt, agent_info, persona_override=None, force_one_line=False):
    if not agent_info: return "분석 데이터 없음"
    role  = persona_override or agent_info.get('agent_role', 'Assistant')
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오.)"
    fp    = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라) {prompt}" if force_one_line else prompt + guard

    for attempt in range(3):
        try:
            res    = google_genai.models.generate_content(
                model=_DEFAULT_MODEL,
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {fp}"
            )
            # 비용 기록
            try:
                usage = res.usage_metadata
                record_cost(
                    call_type     = agent_info.get('agent_role', 'UNKNOWN'),
                    input_tokens  = getattr(usage, 'prompt_token_count',     0),
                    output_tokens = getattr(usage, 'candidates_token_count', 0),
                )
            except: pass
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
# Gemini 호출 — JSON 전용
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
                model=_DEFAULT_MODEL,
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {full_prompt}"
            )
            # 비용 기록 — 파싱 성공/실패 무관하게 항상 기록
            try:
                usage = res.usage_metadata
                record_cost(
                    call_type     = agent_info.get('agent_role', 'UNKNOWN'),
                    input_tokens  = getattr(usage, 'prompt_token_count',     0),
                    output_tokens = getattr(usage, 'candidates_token_count', 0),
                )
            except: pass

            raw = res.text.strip()
            # 마크다운 펜스 제거
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$",     "", raw)
            raw = raw.strip()
            # { } 범위 직접 추출 (앞뒤 설명 텍스트 대비)
            brace_start = raw.find('{')
            brace_end   = raw.rfind('}')
            if brace_start != -1 and brace_end != -1:
                raw = raw[brace_start:brace_end + 1]

            return json.loads(raw)

        except json.JSONDecodeError:
            print(f"  ⚠️ [JSON] [{role}] 파싱 실패 ({attempt+1}/3) — 재시도")
            if attempt == 2:
                try:
                    supabase.table("action_logs").insert({
                        "action_type":      "JSON_PARSE_FAIL",
                        "target_word":      role,
                        "execution_method": "Auto",
                        "details":          f"3회 파싱 실패. 원문 앞 100자: {res.text[:100]}"
                    }).execute()
                except: pass
                print(f"  ❌ [JSON] [{role}] 3회 모두 파싱 실패 → fallback 반환")
                return {"summary": res.text.strip().split('\n')[0][:80], "points": [], "deep": []}
            time.sleep(2)
            continue

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
# YouTube API 헬퍼 / 수집 / 캐시 / 컨텍스트 빌더
# ──────────────────────────────────────────────
def _yt_get(url: str, params: dict) -> dict:
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️ [YT API] 오류: {e}")
        return {}

def collect_youtube(keyword: str, max_recent: int = 2, max_popular: int = 2) -> list:
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

        stats_raw = _yt_get(YT_VIDEO_URL, {
            "key":  YOUTUBE_KEY,
            "id":   ",".join(video_ids),
            "part": "statistics",
        })
        stats_map = {
            s["id"]: int(s["statistics"].get("viewCount", 0))
            for s in stats_raw.get("items", [])
        }

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

def get_youtube_with_cache(keyword: str) -> list:
    try:
        cache = supabase.table("youtube_cache")\
            .select("videos")\
            .eq("keyword", keyword)\
            .eq("cache_date", TODAY)\
            .execute()
        if cache.data:
            print(f"  🎬 [YT Cache] '{keyword}' → 캐시 데이터 재사용")
            return cache.data[0]["videos"]
    except Exception as e:
        print(f"  ⚠️ [YT Cache] 캐시 조회 실패: {e}")

    videos = collect_youtube(keyword)

    try:
        supabase.table("youtube_cache").upsert({
            "keyword":    keyword,
            "cache_date": TODAY,
            "videos":     videos,
        }, on_conflict="keyword,cache_date").execute()
        print(f"  💾 [YT Cache] '{keyword}' → 캐시 저장 완료")
    except Exception as e:
        print(f"  ⚠️ [YT Cache] 캐시 저장 실패: {e}")

    return videos

def build_youtube_context(yt_videos: list) -> str:
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
    if not yt_videos:
        return ""
    cards = ""
    for v in yt_videos[:6]:
        tag     = "⭐ 전문가/인플루언서" if v["is_expert"] else "일반채널"
        tag_clr = "#f59e0b" if v["is_expert"] else "#94a3b8"
        cards += f"""
          <tr>
            <td style="padding:10px 0; border-bottom:1px solid #f0f0f0;">
              <p style="margin:0 0 2px 0; font-size:11px; font-weight:700; color:{tag_clr};">{tag} · {v['keyword']}</p>
              <a href="{v['url']}" style="font-size:14px; font-weight:600; color:#1a1a1a; text-decoration:none; line-height:1.4;">{v['title']}</a>
              <p style="margin:4px 0 0 0; font-size:12px; color:#94a3b8;">{v['channel']} · 구독 {v['subscriber_count']:,} · 조회 {v['view_count']:,}</p>
            </td>
          </tr>"""
    return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
          <tr><td style="padding-bottom:12px;">
            <h2 style="margin:0 0 0 0; font-size:18px; font-weight:700; color:#111;">🎬 유튜브 인사이트</h2>
          </td></tr>
          {cards}
        </table>"""

# ──────────────────────────────────────────────
# GitHub 동기화
# ──────────────────────────────────────────────
def _run_cmd(cmd: str):
    res = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if res.returncode != 0:
        out = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"명령 실패: {cmd} :: {out[:240]}")
    return res

def sync_data_to_github():
    try:
        print("📁 [Sync] GitHub 저장소 동기화 시작...")
        res = supabase.table("reports").select("*").eq("report_date", TODAY).execute()
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(res.data, f, ensure_ascii=False, indent=2)

        _run_cmd('git config --global user.name "Fitz-Dev"')
        _run_cmd('git config --global user.email "positivecha@gmail.com"')
        _run_cmd('git add data.json')

        # 파일 변경이 없으면 불필요한 빈 커밋을 만들지 않는다.
        staged = subprocess.run(
            "git diff --cached --quiet -- data.json",
            shell=True
        ).returncode
        if staged == 0:
            print("ℹ️ [Sync] data.json 변경 없음 — push 스킵")
            return

        _run_cmd(f'git commit -m "📊 [Data Sync] {TODAY} Insights Update"')
        branch = os.environ.get("GITHUB_REF_NAME") or "main"
        _run_cmd(f"git push origin HEAD:{branch}")
        print("🚀 [Sync] GitHub data.json 갱신 완료")
    except Exception as e:
        print(f"🚨 [Sync] 동기화 실패: {e}")

# ──────────────────────────────────────────────
# [1] DEV 엔진: 지정 backlog 작업 집행
# ──────────────────────────────────────────────
def _validate_generated_code(file_path: str, new_code: str):
    compile(new_code, file_path, "exec")

    # 핵심 런타임 파일은 구조가 유지돼야 한다. (문법 통과만으로는 불충분)
    if os.path.basename(file_path) != "news_bot.py":
        return

    required = [
        "def run_autonomous_engine(",
        "def run_agent_initiative(",
        "if __name__ == \"__main__\":",
    ]
    missing = [sig for sig in required if sig not in new_code]
    if missing:
        raise ValueError(f"핵심 구조 누락: {', '.join(missing)}")

    if len(new_code.splitlines()) < 300:
        raise ValueError("핵심 런타임 코드가 비정상적으로 축소되어 배포 차단")


def run_self_evolution(backlog_id: str):
    task     = None
    cur_code = None
    file_path = None

    def _notify(subject, body, is_fail=False):
        icon = "🚨" if is_fail else "✅"
        try:
            _send_gmail(
                to      = "positivecha@gmail.com",
                subject = f"{icon} [DEV] {subject}",
                html    = f"<pre style='font-family:monospace'>{body}</pre>",
            )
        except Exception as mail_err:
            print(f"  ⚠️ [DEV] 알림 이메일 발송 실패: {mail_err}")
            try:
                supabase.table("action_logs").insert({
                    "action_type":      "DEV_NOTIFY_FAIL",
                    "target_word":      subject,
                    "execution_method": "Auto",
                    "details":          str(mail_err)[:200]
                }).execute()
            except: pass

    if not backlog_id:
        print("ℹ️ [DEV] backlog_id 미지정 — 코드 배포 단계 스킵")
        return

    try:
        task_res = supabase.table("dev_backlog").select("*")\
            .eq("id", backlog_id).limit(1).execute()
        if not task_res.data:
            return print(f"💤 [DEV] backlog_id={backlog_id} 작업 없음 — 스킵")

        task      = task_res.data[0]
        status    = (task.get("status") or "").upper()
        if status not in {"CONFIRMED", "DEVELOPING"}:
            return print(f"💤 [DEV] backlog_id={backlog_id} 상태={status} — 배포 스킵")

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
            _validate_generated_code(file_path, new_code)
            print(f"  ✅ [DEV] 구조/문법 검사 통과")
        except Exception as syn_err:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cur_code)
            print(f"  🚨 [DEV] 검증 실패 감지 → 롤백 완료, push 차단")
            err_detail = (
                f"작업: {task['title']}\n"
                f"오류 유형: {type(syn_err).__name__}\n"
                f"내용: {str(syn_err)}\n\n"
                f"조치: 원본 코드로 자동 롤백 완료. GitHub push는 차단되었습니다.\n"
                f"백업 ID는 Supabase code_backups 테이블에서 확인하세요."
            )
            _notify(f"검증 오류 감지 — '{task['title']}' 롤백 완료", err_detail, is_fail=True)
            try:
                supabase.table("action_logs").insert({
                    "action_type":      "DEV_VALIDATE_ROLLBACK",
                    "target_word":      task['title'],
                    "execution_method": "Auto",
                    "details":          f"{type(syn_err).__name__}: {str(syn_err)}"[:200]
                }).execute()
            except: pass
            supabase.table("dev_backlog").update({"status": "VALIDATION_ERROR"})\
                .eq("id", task['id']).execute()
            return

        if new_code == cur_code:
            print("ℹ️ [DEV] 코드 변경 없음 — 커밋/푸시 스킵")
            supabase.table("dev_backlog").update({
                "status":       "COMPLETED",
                "completed_at": NOW.isoformat()
            }).eq("id", task['id']).execute()
            return

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_code)

        safe_title = re.sub(r"\s+", " ", str(task.get("title", "Untitled"))).strip()
        safe_title = safe_title.replace('"', "'")[:70]
        _run_cmd('git config --global user.name "Fitz-Dev"')
        _run_cmd('git config --global user.email "positivecha@gmail.com"')
        _run_cmd(f'git add "{file_path}"')
        _run_cmd(f'git commit -m "🤖 [v17.6] {safe_title} (backlog:{backlog_id})"')
        branch = os.environ.get("GITHUB_REF_NAME") or "main"
        _run_cmd(f"git push origin HEAD:{branch}")

        supabase.table("dev_backlog").update({
            "status":       "COMPLETED",
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
        if file_path and cur_code:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(cur_code)
                print("  🔁 [DEV] 예외 발생으로 원본 코드 자동 복구 완료")
            except Exception as rb_err:
                print(f"  🚨 [DEV] 원본 코드 복구 실패: {rb_err}")
        if task:
            try:
                supabase.table("dev_backlog").update({"status": "DEPLOY_FAILED"})\
                    .eq("id", task["id"]).execute()
            except: pass
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
            p   = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)",   ref, re.DOTALL)
            r   = re.search(r"\[REASON\](.*?)(?=\[NEEDS_DEV\]|$)",  ref, re.DOTALL)
            nd  = re.search(r"\[NEEDS_DEV\](.*?)$",                  ref, re.DOTALL)
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
# [4] 이메일 발송 — 뉴스레터 템플릿
# ──────────────────────────────────────────────
def _build_email_html(report, yt_videos=None):
    bk        = report.get("by_keyword", {})
    yt_videos = yt_videos or []

    keyword_sections = ""
    kw_list = list(bk.items())

    for idx, (kw, kd) in enumerate(kw_list):
        articles = kd.get("articles", [])
        ba_brief = kd.get("ba_brief", {})

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
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
          <tr>
            <td style="padding-bottom:14px;">
              <span style="display:inline-block; background:#f0f4ff; color:#2563eb; font-size:11px; font-weight:700; padding:4px 10px; border-radius:20px; letter-spacing:.5px;"># {kw}</span>
            </td>
          </tr>
          <tr><td>{article_rows and f'<table width="100%" cellpadding="0" cellspacing="0">{article_rows}</table>' or ''}</td></tr>
          {f'<tr><td style="padding-top:14px;"><ul style="margin:0; padding-left:18px;">{ba_html}</ul></td></tr>' if ba_html else ''}
        </table>
        {divider}"""

    yt_block        = build_youtube_email_block(yt_videos)
    dashboard_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a; border-radius:10px; margin-top:32px;">
          <tr>
            <td style="padding:28px 32px; text-align:center;">
              <p style="margin:0 0 16px 0; font-size:18px; font-weight:700; color:#fff;">오늘의 전체 인사이트 확인하기</p>
              <a href="{DASHBOARD_URL}" style="display:inline-block; background:#e8472a; color:#fff; font-size:14px; font-weight:700; padding:14px 32px; border-radius:10px; text-decoration:none; letter-spacing:.5px;">📊 메인 바로가기 →</a>
            </td>
          </tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#f4f4f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5; padding:32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">
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
          <tr>
            <td style="background:#fff; padding:32px;">
              {keyword_sections}
              {yt_block}
              {dashboard_block}
            </td>
          </tr>
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
        _send_gmail(
            to      = user_email,
            subject = f"[{TODAY}] Fitz 비즈니스 인사이트 리포트",
            html    = html,
        )
        print(f"  📧 [Email] {user_email} 발송 완료")
    except Exception as e:
        print(f"  🚨 [Email] 발송 실패: {e}")

# ──────────────────────────────────────────────
# [5] 자율 분석 엔진
# ──────────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v17.5 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    for user in (user_res.data or []):
        try:
            user_id    = user['id']
            user_email = user.get('email', 'Unknown')
            keywords   = user.get('keywords', [])[:5]
            if not keywords: continue

            chk = supabase.table("reports").select("id, email_sent").eq("user_id", user_id).eq("report_date", TODAY).execute()
            if chk.data and chk.data[0].get("email_sent"):
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

                print(f"  🎬 [{word}] YouTube 수집 중...")
                yt_videos = get_youtube_with_cache(word)
                all_yt.extend(yt_videos)
                yt_ctx = build_youtube_context(yt_videos)

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
                    "youtube_videos": yt_videos,
                }

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
                try:
                    supabase.table("reports").update({"email_sent": True})\
                        .eq("id", report_id).execute()
                except Exception as e:
                    print(f"  ⚠️ [Email] email_sent 업데이트 실패: {e}")
                print(f"✅ [{user_email}] 리포트 저장 및 이메일 발송 완료 (YouTube {len(all_yt)}개 포함)")

        except Exception as e:
            print(f"❌ 유저 에러 ({user_email}): {e}")
            continue

    record_supabase_stats()
    sync_data_to_github()
    run_agent_initiative(by_keyword_all=_collect_all_by_keyword(user_res.data or []))


def _collect_all_by_keyword(users: list) -> dict:
    merged = {}
    try:
        res = supabase.table("reports").select("content").eq("report_date", TODAY).execute()
        for r in (res.data or []):
            for kw, kd in (r.get("content", {}).get("by_keyword", {}) or {}).items():
                if kw not in merged:
                    merged[kw] = kd
    except: pass
    return merged

# ──────────────────────────────────────────────
# [6] 산업군 자동 모니터링
# ──────────────────────────────────────────────
def run_industry_monitor():
    print("🏭 [Industry] 산업군 모니터링 시작...")
    agents = get_agents()

    try:
        industries = supabase.table("industry_list")\
            .select("*").eq("is_active", True).execute()
        if not industries.data:
            print("  ⚠️ [Industry] 등록된 산업군 없음")
            return
    except Exception as e:
        print(f"  ❌ [Industry] 산업군 목록 로드 실패: {e}")
        return

    for ind in industries.data:
        industry = ind["industry"]
        category = ind["category"]
        keywords = ind["keywords"]

        try:
            chk = supabase.table("industry_monitor")\
                .select("id").eq("industry", industry)\
                .eq("monitor_date", TODAY).execute()
            if chk.data:
                print(f"  ⏭️ [Industry] '{industry}' 오늘 이미 수집됨 — 스킵")
                continue
        except: pass

        all_articles = []
        for kw in keywords[:2]:
            try:
                gn   = GNews(language='ko', max_results=5)
                news = gn.get_news(kw)
                for n in (news or []):
                    all_articles.append({
                        "keyword": kw,
                        "title":   n.get("title", ""),
                        "url":     n.get("url", n.get("link", "")),
                    })
            except Exception as e:
                print(f"  ⚠️ [Industry] '{kw}' 뉴스 수집 실패: {e}")

        if not all_articles:
            print(f"  ⚠️ [Industry] '{industry}' 뉴스 없음 — 스킵")
            continue

        ctx = "\n".join([f"- {a['title']}" for a in all_articles[:10]])
        try:
            summary = call_agent(
                f"산업군: {industry} ({category})\n오늘 주요 뉴스:\n{ctx}\n\n"
                f"위 뉴스를 바탕으로 {industry} 산업의 오늘 핵심 동향을 3줄로 요약하라.",
                agents.get("BA", agents.get("BRIEF")),
                force_one_line=False
            )
        except:
            summary = "요약 생성 실패"

        try:
            supabase.table("industry_monitor").upsert({
                "industry":     industry,
                "category":     category,
                "articles":     all_articles,
                "summary":      summary,
                "monitor_date": TODAY,
            }, on_conflict="industry,monitor_date").execute()
            print(f"  ✅ [Industry] '{industry}' 동향 저장 완료 ({len(all_articles)}건)")
        except Exception as e:
            print(f"  ❌ [Industry] '{industry}' 저장 실패: {e}")

    print("🏭 [Industry] 산업군 모니터링 완료")

# ──────────────────────────────────────────────
# [7] 에이전트 자율 발의
# ──────────────────────────────────────────────
def run_agent_initiative(by_keyword_all: dict):
    run_industry_monitor()
    print("🧠 [Initiative] 에이전트 자율 발의 시작...")
    agents = get_agents()

    ctx_lines = []
    for kw, kd in by_keyword_all.items():
        articles = kd.get("articles", [])
        titles   = [a.get("title", "") for a in articles[:3]]
        ctx_lines.append(f"[{kw}] " + " / ".join(titles))
    today_ctx = "\n".join(ctx_lines) if ctx_lines else "오늘 수집된 데이터 없음"

    try:
        perf = supabase.table("keyword_performance")\
            .select("keyword, hit_count")\
            .eq("report_date", TODAY).execute()
        perf_lines = [f"{p['keyword']}: {p['hit_count']}건" for p in (perf.data or [])]
        perf_ctx = "\n".join(perf_lines) if perf_lines else "성과 데이터 없음"
    except:
        perf_ctx = "성과 데이터 없음"

    try:
        ind_res = supabase.table("industry_monitor")\
            .select("industry, summary").eq("monitor_date", TODAY).execute()
        industry_ctx = "\n".join([
            f"[{r['industry']}] {r['summary'][:100]}"
            for r in (ind_res.data or []) if r.get("summary")
        ]) or "산업군 데이터 없음"
    except:
        industry_ctx = "산업군 데이터 없음"

    initiative_prompts = {
        "KW": (
            f"오늘 키워드 성과:\n{perf_ctx}\n\n"
            f"오늘 뉴스 컨텍스트:\n{today_ctx}\n\n"
            f"산업군 동향:\n{industry_ctx}\n\n"
            "위 데이터를 분석하여 유저 키워드를 관리하라.\n"
            "반드시 아래 형식으로만 응답하라:\n"
            "[ADD]추가추천키워드1,추가추천키워드2,추가추천키워드3\n"
            "[REMOVE]제거추천키워드1,제거추천키워드2\n"
            "[REASON]추가/제거 이유를 각각 키워드별로 한 줄씩 설명\n\n"
            "ADD 기준: 산업군 동향에서 급부상 중이거나 뉴스 밀도가 높은 키워드\n"
            "REMOVE 기준: hit_count 3 이하이거나 오늘 뉴스가 없는 키워드"
        ),
        "QA": (
            f"오늘 브리핑 데이터:\n{today_ctx}\n\n"
            "오늘 리포트의 품질을 100점 만점으로 평가하고, "
            "개선이 필요한 점을 instruction 업데이트 형태로 제안하라. "
            "점수와 근거를 반드시 포함할 것."
        ),
        "DATA": (
            f"오늘 뉴스 수집 성과:\n{perf_ctx}\n\n"
            "뉴스 수집량이 적은 키워드나 품질 이슈를 분석하고 "
            "데이터 수집 전략 개선안을 instruction 업데이트 형태로 제안하라."
        ),
        "BA": (
            f"오늘 분석 컨텍스트:\n{today_ctx}\n\n"
            "오늘 비즈니스 분석에서 부족했던 점을 파악하고 "
            "더 날카로운 인사이트를 제공하기 위한 instruction 개선안을 제안하라."
        ),
        "MASTER": (
            f"오늘 전체 시스템 성과:\n키워드 성과:\n{perf_ctx}\n\n뉴스 컨텍스트:\n{today_ctx}\n\n"
            "전체 에이전트 시스템의 오늘 성과를 종합 평가하고, "
            "가장 시급한 개발 또는 개선 안건 1가지를 dev_backlog 등록 형태로 제안하라. "
            "제안 형식: [TITLE]안건제목 [DETAIL]상세요구사항"
        ),
    }

    for role, prompt in initiative_prompts.items():
        agent_info = agents.get(role)
        if not agent_info:
            continue
        try:
            print(f"  🤖 [{role}] 자율 발의 생성 중...")
            proposal = call_agent(prompt, agent_info, force_one_line=False)

            if not proposal or proposal in ["분석 지연 중", "분석 데이터 없음"]:
                print(f"  ⚠️ [{role}] 발의 내용 없음 — 스킵")
                continue

            if role == "KW":
                add_m    = re.search(r"\[ADD\](.*?)(?=\[REMOVE\]|\[REASON\]|$)",    proposal, re.DOTALL)
                remove_m = re.search(r"\[REMOVE\](.*?)(?=\[ADD\]|\[REASON\]|$)",    proposal, re.DOTALL)
                reason_m = re.search(r"\[REASON\](.*?)$",                            proposal, re.DOTALL)

                add_kws    = [k.strip() for k in (add_m.group(1).split(",") if add_m else []) if k.strip()]
                remove_kws = [k.strip() for k in (remove_m.group(1).split(",") if remove_m else []) if k.strip()]
                reason     = reason_m.group(1).strip() if reason_m else "KW 에이전트 자율 분석"

                if not add_kws and not remove_kws:
                    print(f"  ⚠️ [KW] 파싱 실패 — 원문 등록")
                    supabase.table("pending_approvals").insert({
                        "agent_role":           "KW",
                        "proposed_instruction": proposal,
                        "proposal_reason":      f"{TODAY} KW 자율 발의 (파싱 실패)",
                        "needs_dev":            False,
                        "status":               "PENDING",
                    }).execute()
                    continue

                structured = (
                    f"[키워드 관리 제안]\n"
                    f"✅ 추가 추천: {', '.join(add_kws) if add_kws else '없음'}\n"
                    f"❌ 제거 추천: {', '.join(remove_kws) if remove_kws else '없음'}\n\n"
                    f"[근거]\n{reason}"
                )
                supabase.table("pending_approvals").insert({
                    "agent_role":           "KW",
                    "proposed_instruction": structured,
                    "proposal_reason":      f"{TODAY} 키워드 추가/제거 제안 — 추가 {len(add_kws)}개 / 제거 {len(remove_kws)}개",
                    "needs_dev":            False,
                    "status":               "PENDING",
                }).execute()
                print(f"  ✅ [KW] 키워드 제안 등록 완료 — 추가 {len(add_kws)}개 / 제거 {len(remove_kws)}개")
                continue

            if role == "MASTER":
                t = re.search(r"\[TITLE\](.*?)(?=\[DETAIL\]|$)",  proposal, re.DOTALL)
                d = re.search(r"\[DETAIL\](.*?)$",                 proposal, re.DOTALL)
                if t and d:
                    title  = t.group(1).strip()
                    detail = d.group(1).strip()
                    supabase.table("dev_backlog").insert({
                        "title":         f"[AI발의] {title}",
                        "task_detail":   detail,
                        "affected_file": "news_bot.py",
                        "priority":      5,
                        "status":        "PENDING",
                    }).execute()
                    print(f"  📋 [MASTER] dev_backlog 자동 등록: {title}")
                continue

            supabase.table("pending_approvals").insert({
                "agent_role":           role,
                "proposed_instruction": proposal,
                "proposal_reason":      f"{TODAY} 브리핑 데이터 기반 자율 발의",
                "needs_dev":            False,
                "status":               "PENDING",
            }).execute()
            print(f"  ✅ [{role}] 자율 발의 등록 완료 → HQ 결재 대기")

        except Exception as e:
            print(f"  ❌ [{role}] 자율 발의 실패: {e}")

    print("🧠 [Initiative] 자율 발의 완료 — HQ에서 확인하세요")

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
        run_self_evolution(CURRENT_BACKLOG_ID)
        run_autonomous_engine()
