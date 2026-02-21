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
ADMIN_MAIL = "positivecha@gmail.com"

supabase: Client = create_client(SB_URL, SB_KEY)
google_genai     = genai.Client(api_key=GEMINI_KEY)

# ──────────────────────────────────────────────
# [시작 시점] 필수 환경변수 체크
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
    else:
        print("✅ [ENV] 환경변수 전체 확인 완료")

_check_env()

DASHBOARD_URL = "https://fitzcha.github.io/newsbot/app.html"

YT_SEARCH_URL  = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEO_URL   = "https://www.googleapis.com/youtube/v3/videos"
YT_CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"

EXPERT_SUBSCRIBER_THRESHOLD = 100_000

# ──────────────────────────────────────────────
# [과금] Gemini 단가 + 누적 카운터
# ──────────────────────────────────────────────
_GEMINI_PRICE = {
    "gemini-2.0-flash": {"input": 0.000075, "output": 0.0003},
}
_AVG_INPUT_TOKENS  = 800
_AVG_OUTPUT_TOKENS = 300
_gemini_call_count = 0
_gemini_cost_usd   = 0.0


# ══════════════════════════════════════════════
# [공통] 재시도 래퍼
# ══════════════════════════════════════════════
def _retry(fn, label="", max_attempts=3, base_wait=5, retryable_codes=("429","500","503","502")):
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            last_err = e
            is_retryable = any(code in err for code in retryable_codes)
            if is_retryable and attempt < max_attempts - 1:
                wait = base_wait * (2 ** attempt)
                print(f"  ⏳ [{label}] 재시도 대기 {wait}s ({attempt+1}/{max_attempts}) — {err[:60]}")
                time.sleep(wait)
            else:
                raise last_err
    raise last_err


def _sb_write(fn, label=""):
    return _retry(fn, label=f"SB:{label}", max_attempts=3, base_wait=3,
                  retryable_codes=("500","503","502","timeout","connection"))


# ──────────────────────────────────────────────
# [공통] Gmail SMTP
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


def _send_gmail_retry(to, subject: str, html: str, max_attempts=3):
    def _try(): _send_gmail(to, subject, html)
    _retry(_try, label=f"Gmail:{subject[:30]}", max_attempts=max_attempts,
           base_wait=10, retryable_codes=("SMTPException","Connection","timeout","SMTP"))


# ══════════════════════════════════════════════
# [공통] 파이프라인 장애 감지 + 알림
# ══════════════════════════════════════════════
_pipeline_errors: list[dict] = []

def _record_error(stage: str, target: str, err: Exception | str):
    msg = str(err)
    _pipeline_errors.append({"stage": stage, "target": target, "error": msg})
    print(f"  🔴 [FAULT] {stage} | {target} | {msg[:80]}")
    try:
        supabase.table("action_logs").insert({
            "action_type":      "PIPELINE_ERROR",
            "target_word":      target,
            "execution_method": stage,
            "details":          msg[:300],
        }).execute()
    except: pass


def _send_pipeline_summary(stats: dict):
    ok    = stats.get("success", 0)
    fail  = stats.get("failed",  0)
    skip  = stats.get("skipped", 0)
    total = stats.get("total",   0)
    g_calls = stats.get("gemini_calls", 0)
    g_cost  = stats.get("gemini_cost",  0.0)

    status_icon = "✅" if fail == 0 else ("⚠️" if ok > 0 else "🚨")
    subject = f"{status_icon} [{TODAY}] Fitz 브리핑 완료 — 성공 {ok}/{total}"

    error_rows = ""
    for e in _pipeline_errors:
        error_rows += (
            f"<tr><td style='padding:4px 8px;color:#dc2626;font-weight:700'>{e['stage']}</td>"
            f"<td style='padding:4px 8px'>{e['target']}</td>"
            f"<td style='padding:4px 8px;color:#666;font-size:12px'>{e['error'][:120]}</td></tr>"
        )
    error_section = f"""
        <h3 style='color:#dc2626'>🔴 장애 목록 ({len(_pipeline_errors)}건)</h3>
        <table border='1' cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%;font-size:13px'>
          <tr style='background:#fee2e2'><th>구간</th><th>대상</th><th>오류 내용</th></tr>
          {error_rows}
        </table>""" if _pipeline_errors else "<p style='color:#16a34a'>✅ 장애 없음</p>"

    kw_ok   = ", ".join(stats.get("keyword_ok",   [])) or "없음"
    kw_fail = ", ".join(stats.get("keyword_fail", [])) or "없음"

    html = f"""
    <div style='font-family:sans-serif;max-width:640px;margin:0 auto'>
      <h2 style='background:#0f172a;color:#fff;padding:16px;border-radius:8px 8px 0 0;margin:0'>
        {status_icon} Fitz 브리핑 파이프라인 리포트
      </h2>
      <div style='background:#f8fafc;padding:20px;border:1px solid #e2e8f0'>
        <table style='width:100%;font-size:14px'>
          <tr><td>📅 날짜</td><td><b>{TODAY}</b></td></tr>
          <tr><td>👥 전체 유저</td><td><b>{total}명</b></td></tr>
          <tr><td>✅ 성공</td><td style='color:#16a34a'><b>{ok}명</b></td></tr>
          <tr><td>❌ 실패</td><td style='color:#dc2626'><b>{fail}명</b></td></tr>
          <tr><td>⏭️ 스킵</td><td style='color:#9333ea'><b>{skip}명</b></td></tr>
          <tr><td>🤖 Gemini 호출</td><td><b>{g_calls}회</b></td></tr>
          <tr><td>💰 추정 비용</td><td style='color:#d97706'><b>${g_cost:.4f} USD</b></td></tr>
        </table>
        <hr>
        <p><b>✅ 성공 키워드:</b> {kw_ok}</p>
        <p><b>❌ 실패 키워드:</b> {kw_fail}</p>
        <hr>
        {error_section}
      </div>
    </div>"""

    try:
        _send_gmail(ADMIN_MAIL, subject, html)
        print(f"📊 [Summary] 파이프라인 완료 알림 발송 → {ADMIN_MAIL}")
    except Exception as e:
        print(f"  ⚠️ [Summary] 알림 발송 실패: {e}")


# ──────────────────────────────────────────────
# [보조] 로그 / 성과 기록
# ──────────────────────────────────────────────
def log_to_db(user_id, target_word, action="분석", method="Auto"):
    try:
        _sb_write(lambda: supabase.table("action_logs").insert({
            "user_id": user_id, "action_type": action,
            "target_word": target_word, "execution_method": method, "details": "Success"
        }).execute(), label="log_to_db")
    except Exception as e:
        print(f"  ⚠️ [log_to_db] 기록 실패: {e}")

def record_performance(user_id, keyword, count):
    try:
        _sb_write(lambda: supabase.table("keyword_performance").insert({
            "user_id": user_id, "keyword": keyword,
            "hit_count": count, "report_date": TODAY
        }).execute(), label="record_perf")
    except Exception as e:
        print(f"  ⚠️ [record_performance] 기록 실패: {e}")


# ══════════════════════════════════════════════
# [과금] 호출 기록 + Supabase 통계
# ══════════════════════════════════════════════
def record_cost(call_type: str = "text", model: str = "gemini-2.0-flash"):
    global _gemini_call_count, _gemini_cost_usd
    price = _GEMINI_PRICE.get(model, _GEMINI_PRICE["gemini-2.0-flash"])
    cost  = (_AVG_INPUT_TOKENS * price["input"] + _AVG_OUTPUT_TOKENS * price["output"]) / 1000
    _gemini_call_count += 1
    _gemini_cost_usd   += cost
    try:
        _sb_write(lambda: supabase.table("cost_log").insert({
            "log_date":   TODAY,
            "call_type":  call_type,
            "model":      model,
            "call_count": 1,
            "cost_usd":   round(cost, 6),
        }).execute(), label="cost_log")
    except Exception as e:
        print(f"  ⚠️ [Cost] 기록 실패 (비치명): {e}")


def record_supabase_stats():
    tables = ["action_logs", "reports", "keyword_analysis_cache",
              "youtube_cache", "cost_log", "pending_approvals", "dev_backlog"]
    counts = {}
    for t in tables:
        try:
            res = supabase.table(t).select("id", count="exact").execute()
            counts[t] = res.count or 0
        except:
            counts[t] = -1
    try:
        _sb_write(lambda: supabase.table("supabase_stats").upsert({
            "stat_date":  TODAY,
            "row_counts": counts,
            "total_rows": sum(v for v in counts.values() if v >= 0),
        }, on_conflict="stat_date").execute(), label="supabase_stats")
        print(f"📊 [Cost] Supabase row 통계 저장 완료: 총 {sum(v for v in counts.values() if v>=0):,}행")
    except Exception as e:
        print(f"  ⚠️ [Cost] Supabase 통계 저장 실패: {e}")


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

    def _call():
        res = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {fp}"
        )
        return res.text.strip().split('\n')[0] if force_one_line else res.text.strip()

    try:
        result = _retry(_call, label=f"Gemini:{role}", max_attempts=3, base_wait=5)
        record_cost("text")
        return result
    except Exception as e:
        print(f"  ❌ [Gemini:{role}] 최종 실패: {str(e)[:80]}")
        return "분석 지연 중"


# ──────────────────────────────────────────────
# [보조] Gemini 호출 — JSON 전용
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

    def _call():
        res = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {full_prompt}"
        )
        raw = res.text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$",     "", raw)
        return json.loads(raw)

    try:
        result = _retry(_call, label=f"GeminiJSON:{role}", max_attempts=3, base_wait=5)
        record_cost("json")
        return result
    except json.JSONDecodeError:
        try:
            raw_text = google_genai.models.generate_content(
                model='gemini-2.0-flash',
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {full_prompt}"
            ).text.strip()
            return {"summary": raw_text.split('\n')[0][:80], "points": [], "deep": []}
        except: pass
        return {"summary": "분석 지연 중", "points": [], "deep": []}
    except Exception as e:
        print(f"  ❌ [GeminiJSON:{role}] 최종 실패: {str(e)[:80]}")
        return {"summary": "분석 지연 중", "points": [], "deep": []}


# ══════════════════════════════════════════════
# [전략 1] 기사 배치 처리
# ══════════════════════════════════════════════
def call_agent_brief_batch(news_list: list, agents: dict) -> list:
    if not news_list:
        return []

    titles_block = "\n".join([f"{i+1}. {n['title']}" for i, n in enumerate(news_list)])
    batch_prompt = f"""아래 뉴스 {len(news_list)}건을 분석하라.
각 뉴스에 대해 반드시 아래 JSON 배열로만 응답하라. 마크다운·코드블록·설명 텍스트 일절 금지.
[
  {{"idx": 1, "summary": "1줄 핵심 요약 (40자 이내)", "impact": "투자 관점 1줄 전망 (40자 이내)"}},
  ...
]
---
{titles_block}"""

    brief_agent = agents.get('BRIEF')
    if not brief_agent:
        return []

    def _call():
        res = google_genai.models.generate_content(
            model='gemini-2.0-flash',
            contents=(
                f"당신은 {brief_agent.get('agent_role','BRIEF')}입니다.\n"
                f"지침: {brief_agent['instruction']}\n\n"
                f"입력: {batch_prompt}"
            )
        )
        raw = res.text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$",     "", raw)
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("배치 응답이 list가 아님")
        return parsed

    try:
        parsed = _retry(_call, label="Batch", max_attempts=3, base_wait=5)
        record_cost("batch")
        if len(parsed) == len(news_list):
            return parsed
        result_map = {item.get("idx", i+1): item for i, item in enumerate(parsed)}
        return [result_map.get(i+1, {"idx": i+1, "summary": "", "impact": ""})
                for i in range(len(news_list))]
    except Exception as e:
        print(f"  ⚠️ [Batch] 최종 실패 — fallback 예정: {str(e)[:60]}")
        return []


# ══════════════════════════════════════════════
# [전략 3] 키워드 분석 캐시
# ══════════════════════════════════════════════
def get_keyword_analysis_cache(word: str) -> dict | None:
    try:
        res = supabase.table("keyword_analysis_cache") \
            .select("result").eq("cache_key", f"{word}_{TODAY}").execute()
        if res.data:
            print(f"  ♻️  [{word}] 캐시 히트")
            return res.data[0]["result"]
    except Exception as e:
        print(f"  ⚠️ [KW Cache] 조회 실패: {e}")
    return None

def set_keyword_analysis_cache(word: str, result: dict):
    try:
        _sb_write(lambda: supabase.table("keyword_analysis_cache").upsert({
            "cache_key":  f"{word}_{TODAY}",
            "keyword":    word,
            "cache_date": TODAY,
            "result":     result,
        }, on_conflict="cache_key").execute(), label="kw_cache")
        print(f"  💾 [KW Cache] '{word}' 저장 완료")
    except Exception as e:
        _record_error("KW_CACHE_WRITE", word, e)


# ──────────────────────────────────────────────
# [YouTube] API 헬퍼 / 수집 / 캐시 / 컨텍스트 빌더
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
        return []
    results, seen_ids = [], set()
    for order_type, max_n in [("date", max_recent), ("viewCount", max_popular)]:
        raw = _yt_get(YT_SEARCH_URL, {
            "key": YOUTUBE_KEY, "q": keyword, "part": "snippet",
            "type": "video", "order": order_type, "maxResults": max_n,
            "relevanceLanguage": "ko", "regionCode": "KR",
            "publishedAfter": (NOW - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z"),
        })
        items = raw.get("items", [])
        if not items: continue
        video_ids   = [it["id"]["videoId"] for it in items if it["id"].get("videoId")]
        channel_ids = list({it["snippet"]["channelId"] for it in items})
        stats_map = {
            s["id"]: int(s["statistics"].get("viewCount", 0))
            for s in _yt_get(YT_VIDEO_URL, {"key": YOUTUBE_KEY, "id": ",".join(video_ids), "part": "statistics"}).get("items", [])
        }
        ch_map = {
            c["id"]: int(c["statistics"].get("subscriberCount", 0))
            for c in _yt_get(YT_CHANNEL_URL, {"key": YOUTUBE_KEY, "id": ",".join(channel_ids), "part": "statistics"}).get("items", [])
        }
        for it in items:
            vid = it["id"].get("videoId")
            if not vid or vid in seen_ids: continue
            seen_ids.add(vid)
            sn, ch_id = it["snippet"], it["snippet"]["channelId"]
            subs = ch_map.get(ch_id, 0)
            results.append({
                "title": sn["title"], "channel": sn["channelTitle"],
                "channel_id": ch_id, "video_id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "published": sn.get("publishedAt", "")[:10],
                "view_count": stats_map.get(vid, 0),
                "subscriber_count": subs,
                "is_expert": subs >= EXPERT_SUBSCRIBER_THRESHOLD,
                "order_type": "최신" if order_type == "date" else "인기",
                "keyword": keyword,
            })
    print(f"  🎬 [YT] '{keyword}' → {len(results)}개 수집")
    return results

def get_youtube_with_cache(keyword: str) -> list:
    try:
        cache = supabase.table("youtube_cache") \
            .select("videos").eq("keyword", keyword).eq("cache_date", TODAY).execute()
        if cache.data:
            print(f"  🎬 [YT Cache] '{keyword}' 재사용")
            return cache.data[0]["videos"]
    except Exception as e:
        print(f"  ⚠️ [YT Cache] 조회 실패: {e}")
    videos = collect_youtube(keyword)
    try:
        _sb_write(lambda: supabase.table("youtube_cache").upsert(
            {"keyword": keyword, "cache_date": TODAY, "videos": videos},
            on_conflict="keyword,cache_date"
        ).execute(), label="yt_cache")
    except Exception as e:
        print(f"  ⚠️ [YT Cache] 저장 실패: {e}")
    return videos

def build_youtube_context(yt_videos: list) -> str:
    if not yt_videos: return ""
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
    if not yt_videos: return ""
    cards = ""
    for v in yt_videos[:4]:
        tag_html = (
            '<span style="background:#fef3c7;color:#92400e;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;">⭐ 전문가/인플루언서</span>'
            if v["is_expert"] else ""
        )
        cards += f"""
          <tr><td style="padding:12px 0;border-bottom:1px solid #f0f0f0;">
            <p style="margin:0 0 4px;font-size:14px;font-weight:600;color:#1a1a1a">{v['title']}</p>
            <p style="margin:0 0 6px;font-size:12px;color:#666">{v['channel']} · 조회 {v['view_count']:,} · {v['published']}</p>
            {tag_html}
            <a href="{v['url']}" style="display:inline-block;margin-top:6px;font-size:12px;color:#2563eb;font-weight:700;text-decoration:none;">▶ 영상 보기 →</a>
          </td></tr>"""
    return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
          <tr><td><h2 style="margin:0 0 16px;font-size:18px;font-weight:700;color:#111;">🎬 유튜브 인사이트</h2></td></tr>
          {cards}
        </table>"""


# ──────────────────────────────────────────────
# [보조] GitHub 동기화
# ──────────────────────────────────────────────
def sync_data_to_github():
    try:
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
        _record_error("GITHUB_SYNC", TODAY, e)


# ──────────────────────────────────────────────
# [1] DEV 엔진
# ──────────────────────────────────────────────
def run_self_evolution():
    task = None
    def _notify(subject, body, is_fail=False):
        icon = "🚨" if is_fail else "✅"
        try:
            _send_gmail(ADMIN_MAIL, f"{icon} [DEV] {subject}",
                        f"<pre style='font-family:monospace'>{body}</pre>")
        except Exception as e:
            print(f"  ⚠️ [DEV] 알림 발송 실패: {e}")
            try:
                supabase.table("action_logs").insert({
                    "action_type": "DEV_NOTIFY_FAIL", "target_word": subject,
                    "execution_method": "Auto", "details": str(e)[:200]
                }).execute()
            except: pass

    try:
        task_res = supabase.table("dev_backlog").select("*") \
            .eq("status", "CONFIRMED").order("priority").limit(1).execute()
        if not task_res.data:
            return print("💤 [DEV] 실행 확정 대기 작업 없음.")

        task      = task_res.data[0]
        file_path = task.get('affected_file', 'news_bot.py')
        print(f"🛠️ [DEV] 착수: {task['title']}")

        with open(file_path, "r", encoding="utf-8") as f:
            cur_code = f.read()

        try:
            _sb_write(lambda: supabase.table("code_backups").insert({
                "file_path": file_path, "code": cur_code,
                "task_id": task['id'], "task_title": task['title'],
                "backed_up_at": NOW.isoformat()
            }).execute(), label="code_backup")
        except Exception as bk_err:
            _notify(f"백업 실패 — '{task['title']}' 중단", str(bk_err), is_fail=True)
            supabase.table("dev_backlog").update({"status": "BACKUP_FAILED"}).eq("id", task['id']).execute()
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
        except SyntaxError as syn_err:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(cur_code)
            _notify(f"문법 오류 — '{task['title']}' 롤백",
                    f"SyntaxError line {syn_err.lineno}: {syn_err.msg}", is_fail=True)
            supabase.table("dev_backlog").update({"status": "SYNTAX_ERROR"}).eq("id", task['id']).execute()
            return

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_code)
        for cmd in [
            'git config --global user.name "Fitz-Dev"',
            'git config --global user.email "positivecha@gmail.com"',
            'git add .', f'git commit -m "🤖 [v18] {task["title"]}"', 'git push'
        ]:
            subprocess.run(cmd, shell=True)

        supabase.table("dev_backlog").update({
            "status": "COMPLETED", "completed_at": NOW.isoformat()
        }).eq("id", task['id']).execute()
        _notify(f"배포 완료 — '{task['title']}'",
                f"파일: {file_path}\n시각: {NOW.strftime('%Y-%m-%d %H:%M')} KST")

    except Exception as e:
        _record_error("DEV_ENGINE", task['title'] if task else "unknown", e)
        if task:
            _notify(f"예상치 못한 오류 — '{task.get('title','')}'", str(e), is_fail=True)


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
                "[PROPOSAL]수정지침 [REASON]수정근거 [NEEDS_DEV]YES or NO"
            )
            ref = call_agent(rp, info, "Insight Evolver")
            p   = re.search(r"\[PROPOSAL\](.*?)(?=\[REASON\]|$)",   ref, re.DOTALL)
            r   = re.search(r"\[REASON\](.*?)(?=\[NEEDS_DEV\]|$)",  ref, re.DOTALL)
            nd  = re.search(r"\[NEEDS_DEV\](.*?)$",                  ref, re.DOTALL)
            if p:
                needs_dev = "YES" in (nd.group(1).strip().upper() if nd else "NO")
                try:
                    _sb_write(lambda: supabase.table("pending_approvals").insert({
                        "agent_role": role,
                        "proposed_instruction": p.group(1).strip(),
                        "proposal_reason": r.group(1).strip() if r else "VOC 피드백 반영",
                        "needs_dev": needs_dev
                    }).execute(), label="self_reflection")
                except Exception as e:
                    _record_error("SELF_REFLECTION", role, e)
    except Exception as e:
        print(f"  ⚠️ [Reflection] 실패: {e}")


# ──────────────────────────────────────────────
# [3] 데드라인 자동 승인
# ──────────────────────────────────────────────
def manage_deadline_approvals():
    if NOW.hour == 23 and NOW.minute >= 30:
        try:
            pending = supabase.table("pending_approvals").select("*").eq("status", "PENDING").execute()
            for item in (pending.data or []):
                try:
                    _sb_write(lambda: supabase.table("agents").update({
                        "instruction": item['proposed_instruction']
                    }).eq("agent_role", item['agent_role']).execute(), label="approval_apply")
                    _sb_write(lambda: supabase.table("pending_approvals").update({
                        "status": "APPROVED"
                    }).eq("id", item['id']).execute(), label="approval_status")

                    if item.get('needs_dev'):
                        dup = supabase.table("dev_backlog").select("id") \
                            .eq("source_approval_id", item['id']).execute()
                        if dup.data: continue
                        _sb_write(lambda: supabase.table("dev_backlog").insert({
                            "title":              f"[자동등록] {item['agent_role']} — {item.get('proposal_reason','')[:50]}",
                            "task_detail":        item['proposed_instruction'],
                            "affected_file":      "news_bot.py",
                            "priority":           10,
                            "status":             "PENDING_MASTER",
                            "source_approval_id": item['id']
                        }).execute(), label="dev_backlog_insert")
                except Exception as e:
                    _record_error("DEADLINE_APPROVAL", item.get('agent_role','?'), e)
        except Exception as e:
            print(f"🚨 [Approvals] 처리 실패: {e}")


# ──────────────────────────────────────────────
# [4] 이메일 발송
# ──────────────────────────────────────────────
def _build_email_html(report, yt_videos=None):
    bk        = report.get("by_keyword", {})
    yt_videos = yt_videos or []
    kw_list   = list(bk.items())

    keyword_sections = ""
    for idx, (kw, kd) in enumerate(kw_list):
        articles = kd.get("articles", [])
        ba_brief = kd.get("ba_brief", {})
        article_rows = ""
        for a in articles[:3]:
            title      = a.get("title", "")
            pm_summary = a.get("pm_summary", "")
            url        = a.get("url", a.get("link", "#"))
            article_rows += f"""
              <tr><td style="padding:10px 0;border-bottom:1px solid #f0f0f0;">
                <p style="margin:0 0 4px;font-size:14px;font-weight:600;color:#1a1a1a">{title}</p>
                <p style="margin:0 0 6px;font-size:13px;color:#666">{pm_summary}</p>
                <a href="{url}" style="font-size:12px;color:#2563eb;font-weight:700;text-decoration:none;">더 자세히 알아보기 →</a>
              </td></tr>"""
        if isinstance(ba_brief, dict):
            ba_items = ([ba_brief["summary"]] if ba_brief.get("summary") else []) + ba_brief.get("points", [])
        else:
            ba_items = [l.strip() for l in str(ba_brief).split('\n') if l.strip()][:5]
        ba_html = "".join(
            f'<li style="margin-bottom:6px;color:#444;font-size:13px;line-height:1.6">{l}</li>'
            for l in ba_items if l
        )
        divider = """<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:32px;">
                       <tr><td style="border-top:1px solid #f0f0f0;"></td></tr></table>""" \
                  if idx < len(kw_list) - 1 else ""
        keyword_sections += f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
          <tr><td>
            <h2 style="margin:0 0 16px;font-size:18px;font-weight:700;color:#111;">#{kw}</h2>
            <table width="100%" cellpadding="0" cellspacing="0">{article_rows}</table>
            <ul style="margin:16px 0 0;padding-left:20px">{ba_html}</ul>
          </td></tr>
        </table>{divider}"""

    dashboard_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:linear-gradient(135deg,#1e3a5f,#2563eb);border-radius:12px;margin-top:32px;">
          <tr><td style="padding:28px;text-align:center;">
            <p style="margin:0 0 16px;font-size:18px;font-weight:700;color:#fff;">오늘의 전체 인사이트 확인하기</p>
            <a href="{DASHBOARD_URL}" style="display:inline-block;background:#e8472a;color:#fff;font-size:14px;font-weight:700;padding:14px 32px;border-radius:10px;text-decoration:none;">📊 대시보드 바로가기 →</a>
          </td></tr>
        </table>"""

    return f"""<!DOCTYPE html><html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
        <tr><td style="background:#0f172a;border-radius:12px 12px 0 0;padding:28px 32px;">
          <table width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>
              <span style="font-size:11px;font-weight:700;color:#64748b;letter-spacing:2px;text-transform:uppercase">FITZ INTELLIGENCE</span>
              <h1 style="margin:6px 0 0;font-size:22px;font-weight:700;color:#fff">Daily Briefing</h1>
            </td>
            <td align="right" style="vertical-align:top"><span style="font-size:12px;color:#64748b">{TODAY}</span></td>
          </tr></table>
        </td></tr>
        <tr><td style="background:#fff;padding:32px">
          {keyword_sections}
          {build_youtube_email_block(yt_videos)}
          {dashboard_block}
        </td></tr>
        <tr><td style="background:#f8faff;border-radius:0 0 12px 12px;padding:20px 32px;text-align:center;">
          <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6">
            Fitz Intelligence · 매일 오전 9시 자동 발송<br>© 2026 Fitz. All rights reserved.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def send_email_report(user_email, report, yt_videos=None):
    try:
        html = _build_email_html(report, yt_videos or [])
        _send_gmail_retry(user_email, f"[{TODAY}] Fitz 비즈니스 인사이트 리포트", html)
        print(f"  📧 [Email] {user_email} 발송 완료")
    except Exception as e:
        raise


# ──────────────────────────────────────────────
# [5] 자율 분석 엔진
# ──────────────────────────────────────────────
def run_autonomous_engine():
    global _pipeline_errors, _gemini_call_count, _gemini_cost_usd
    _pipeline_errors   = []
    _gemini_call_count = 0
    _gemini_cost_usd   = 0.0

    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v18.1 가동")

    user_res = supabase.table("user_settings").select("*").execute()
    stats    = {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                "keyword_ok": [], "keyword_fail": []}

    for user in (user_res.data or []):
        user_id    = user['id']
        user_email = user.get('email', 'Unknown')
        keywords   = user.get('keywords', [])[:5]
        if not keywords: continue
        stats["total"] += 1

        try:
            chk = supabase.table("reports").select("id, email_sent") \
                .eq("user_id", user_id).eq("report_date", TODAY).execute()
            if chk.data and chk.data[0].get("email_sent"):
                print(f"⏭️  [Skip] {user_email} — 이미 발송 완료")
                stats["skipped"] += 1
                continue

            print(f"🔍 [{user_email}] 키워드 {keywords} 분석 시작")
            by_keyword, all_articles, all_yt = {}, [], []

            for word in keywords:
                # ── [전략 3] 캐시 확인 ──
                cached = get_keyword_analysis_cache(word)
                if cached:
                    by_keyword[word] = cached
                    for a in cached.get("articles", []):
                        all_articles.append(f"[{word}] {a.get('title','')}")
                    all_yt.extend(cached.get("youtube_videos", []))
                    log_to_db(user_id, word, "키워드분석(캐시)")
                    stats["keyword_ok"].append(word)
                    continue

                try:
                    print(f"  📰 [{word}] 뉴스 수집 중...")
                    is_korean = any(ord(c) > 0x1100 for c in word)
                    gn        = GNews(language='ko' if is_korean else 'en', max_results=10)
                    news_list = gn.get_news(word)
                    record_performance(user_id, word, len(news_list))

                    if not news_list:
                        print(f"  ⚠️  [{word}] 뉴스 없음")
                        by_keyword[word] = {
                            "ba_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                            "securities_brief": {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                            "pm_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                            "articles": [], "youtube_videos": [],
                        }
                        continue

                    # ── [전략 1] 배치 처리 ──
                    print(f"  🗞️  [{word}] 배치 분석 중...")
                    batch_results = call_agent_brief_batch(news_list, agents)
                    articles, kw_ctx = [], []

                    if batch_results and len(batch_results) == len(news_list):
                        for i, n in enumerate(news_list):
                            br = batch_results[i]
                            articles.append({**n, "keyword": word,
                                             "pm_summary": br.get("summary") or "요약 없음",
                                             "impact":     br.get("impact")  or "전망 없음"})
                            kw_ctx.append(n['title'])
                            all_articles.append(f"[{word}] {n['title']}")
                    else:
                        print(f"  ⚠️  [{word}] 배치 실패 → 개별 fallback")
                        for n in news_list:
                            pm_summary = call_agent(f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True)
                            impact     = call_agent(f"뉴스: {n['title']}\n전망 1줄.",
                                                    agents.get('STOCK', agents['BRIEF']), force_one_line=True)
                            articles.append({**n, "keyword": word, "pm_summary": pm_summary, "impact": impact})
                            kw_ctx.append(n['title'])
                            all_articles.append(f"[{word}] {n['title']}")

                    yt_videos = get_youtube_with_cache(word)
                    all_yt.extend(yt_videos)
                    ctx = "\n".join(kw_ctx)
                    if yt_ctx := build_youtube_context(yt_videos):
                        ctx += f"\n\n{yt_ctx}"

                    print(f"  🤖 [{word}] 에이전트 분석 중...")
                    kw_result = {
                        "ba_brief": call_agent_json(
                            f"키워드 '{word}' 뉴스 및 유튜브 기반 비즈니스 수익 구조 및 경쟁 분석:\n{ctx}", agents['BA']),
                        "securities_brief": call_agent_json(
                            f"키워드 '{word}' 뉴스 및 유튜브 기반 주식 시장 반응 및 투자 인사이트:\n{ctx}", agents['STOCK']),
                        "pm_brief": call_agent_json(
                            f"키워드 '{word}' 뉴스 및 유튜브 기반 전략적 서비스 기획 브리핑:\n{ctx}", agents['PM']),
                        "articles": articles, "youtube_videos": yt_videos,
                    }
                    by_keyword[word] = kw_result
                    log_to_db(user_id, word, "키워드분석")
                    set_keyword_analysis_cache(word, kw_result)
                    stats["keyword_ok"].append(word)

                except Exception as e:
                    _record_error("KEYWORD_ANALYSIS", word, e)
                    stats["keyword_fail"].append(word)
                    by_keyword[word] = {
                        "ba_brief":         {"summary": f"[분석 실패] {str(e)[:40]}", "points": [], "deep": []},
                        "securities_brief": {"summary": "분석 실패", "points": [], "deep": []},
                        "pm_brief":         {"summary": "분석 실패", "points": [], "deep": []},
                        "articles": [], "youtube_videos": [],
                    }

            if not by_keyword:
                print(f"⚠️  [{user_email}] 분석 결과 없음")
                stats["failed"] += 1
                continue

            all_ctx     = "\n".join(all_articles)
            hr_proposal = call_agent(f"조직 및 인사 관리 제안:\n{all_ctx}", agents['HR'])
            final_report = {"by_keyword": by_keyword, "hr_proposal": hr_proposal}

            try:
                res = _sb_write(lambda: supabase.table("reports").upsert({
                    "user_id": user_id, "report_date": TODAY,
                    "content": final_report, "qa_score": 95
                }, on_conflict="user_id,report_date").execute(), label="report_upsert")
            except Exception as e:
                _record_error("REPORT_SAVE", user_email, e)
                stats["failed"] += 1
                continue

            if res.data:
                report_id = res.data[0]['id']
                run_agent_self_reflection(report_id)
                try:
                    send_email_report(user_email, final_report, all_yt)
                    _sb_write(lambda: supabase.table("reports").update({"email_sent": True})
                              .eq("id", report_id).execute(), label="email_sent_flag")
                    print(f"✅ [{user_email}] 완료")
                    stats["success"] += 1
                except Exception as e:
                    _record_error("EMAIL_SEND", user_email, e)
                    stats["failed"] += 1

        except Exception as e:
            _record_error("USER_PIPELINE", user_email, e)
            stats["failed"] += 1

    sync_data_to_github()
    run_agent_initiative(by_keyword_all=_collect_all_by_keyword(user_res.data or []))

    # ── 과금 통계 저장 + 파이프라인 요약 알림 ──
    record_supabase_stats()
    stats["gemini_calls"] = _gemini_call_count
    stats["gemini_cost"]  = round(_gemini_cost_usd, 4)
    _send_pipeline_summary(stats)


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
        industries = supabase.table("industry_list").select("*").eq("is_active", True).execute()
        if not industries.data:
            return print("  ⚠️ [Industry] 등록된 산업군 없음")
    except Exception as e:
        return print(f"  ❌ [Industry] 로드 실패: {e}")

    for ind in industries.data:
        industry, category, keywords = ind["industry"], ind["category"], ind["keywords"]
        try:
            chk = supabase.table("industry_monitor").select("id") \
                .eq("industry", industry).eq("monitor_date", TODAY).execute()
            if chk.data:
                print(f"  ⏭️ '{industry}' 오늘 이미 수집")
                continue
        except: pass

        all_articles = []
        for kw in keywords[:2]:
            try:
                news = GNews(language='ko', max_results=5).get_news(kw)
                for n in (news or []):
                    all_articles.append({"keyword": kw, "title": n.get("title",""), "url": n.get("url", n.get("link",""))})
            except Exception as e:
                print(f"  ⚠️ [Industry] '{kw}' 실패: {e}")

        if not all_articles: continue

        ctx = "\n".join([f"- {a['title']}" for a in all_articles[:10]])
        try:
            summary = call_agent(
                f"산업군: {industry} ({category})\n오늘 주요 뉴스:\n{ctx}\n\n"
                f"{industry} 산업의 오늘 핵심 동향을 3줄로 요약하라.",
                agents.get("BA", agents.get("BRIEF")), force_one_line=False
            )
        except: summary = "요약 생성 실패"

        try:
            _sb_write(lambda: supabase.table("industry_monitor").upsert({
                "industry": industry, "category": category,
                "articles": all_articles, "summary": summary, "monitor_date": TODAY,
            }, on_conflict="industry,monitor_date").execute(), label="industry_monitor")
            print(f"  ✅ '{industry}' 저장 완료")
        except Exception as e:
            _record_error("INDUSTRY_MONITOR", industry, e)

    print("🏭 [Industry] 완료")


# ──────────────────────────────────────────────
# [7] 에이전트 자율 발의
# ──────────────────────────────────────────────
def run_agent_initiative(by_keyword_all: dict):
    run_industry_monitor()
    print("🧠 [Initiative] 자율 발의 시작...")
    agents = get_agents()

    ctx_lines = []
    for kw, kd in by_keyword_all.items():
        titles = [a.get("title","") for a in kd.get("articles",[])[:3]]
        ctx_lines.append(f"[{kw}] " + " / ".join(titles))
    today_ctx = "\n".join(ctx_lines) or "오늘 수집된 데이터 없음"

    try:
        perf = supabase.table("keyword_performance").select("keyword, hit_count") \
            .eq("report_date", TODAY).execute()
        perf_ctx = "\n".join([f"{p['keyword']}: {p['hit_count']}건" for p in (perf.data or [])]) or "성과 데이터 없음"
    except: perf_ctx = "성과 데이터 없음"

    try:
        ind_res = supabase.table("industry_monitor").select("industry, summary").eq("monitor_date", TODAY).execute()
        industry_ctx = "\n".join([f"[{r['industry']}] {r['summary'][:100]}"
                                   for r in (ind_res.data or []) if r.get("summary")]) or "산업군 데이터 없음"
    except: industry_ctx = "산업군 데이터 없음"

    initiative_prompts = {
        "KW": (
            f"오늘 키워드 성과:\n{perf_ctx}\n\n뉴스 컨텍스트:\n{today_ctx}\n\n산업군 동향:\n{industry_ctx}\n\n"
            "반드시 아래 형식으로만 응답:\n[ADD]키워드1,키워드2\n[REMOVE]키워드1\n[REASON]근거"
        ),
        "QA":   (f"오늘 브리핑:\n{today_ctx}\n\n품질 100점 평가 + instruction 개선안 제안. 점수와 근거 포함."),
        "DATA": (f"수집 성과:\n{perf_ctx}\n\n수집 전략 개선안을 instruction 형태로 제안."),
        "BA":   (f"분석 컨텍스트:\n{today_ctx}\n\n더 날카로운 인사이트를 위한 instruction 개선안 제안."),
        "MASTER": (
            f"키워드 성과:\n{perf_ctx}\n\n뉴스:\n{today_ctx}\n\n"
            "가장 시급한 개선 안건 1가지. 형식: [TITLE]제목 [DETAIL]상세"
        ),
    }

    for role, prompt in initiative_prompts.items():
        agent_info = agents.get(role)
        if not agent_info: continue
        try:
            print(f"  🤖 [{role}] 자율 발의 생성 중...")
            proposal = call_agent(prompt, agent_info, force_one_line=False)
            if not proposal or proposal in ["분석 지연 중", "분석 데이터 없음"]: continue

            if role == "KW":
                add_m    = re.search(r"\[ADD\](.*?)(?=\[REMOVE\]|\[REASON\]|$)",    proposal, re.DOTALL)
                remove_m = re.search(r"\[REMOVE\](.*?)(?=\[ADD\]|\[REASON\]|$)",    proposal, re.DOTALL)
                reason_m = re.search(r"\[REASON\](.*?)$",                            proposal, re.DOTALL)
                add_kws    = [k.strip() for k in (add_m.group(1).split(",")    if add_m    else []) if k.strip()]
                remove_kws = [k.strip() for k in (remove_m.group(1).split(",") if remove_m else []) if k.strip()]
                reason     = reason_m.group(1).strip() if reason_m else "KW 자율 분석"
                structured = (
                    f"[키워드 관리 제안]\n✅ 추가: {', '.join(add_kws) or '없음'}\n❌ 제거: {', '.join(remove_kws) or '없음'}\n\n[근거]\n{reason}"
                    if (add_kws or remove_kws) else proposal
                )
                _sb_write(lambda: supabase.table("pending_approvals").insert({
                    "agent_role": "KW", "proposed_instruction": structured,
                    "proposal_reason": f"{TODAY} 키워드 제안 — 추가 {len(add_kws)}개 / 제거 {len(remove_kws)}개",
                    "needs_dev": False, "status": "PENDING",
                }).execute(), label="kw_initiative")
                continue

            if role == "MASTER":
                t = re.search(r"\[TITLE\](.*?)(?=\[DETAIL\]|$)",  proposal, re.DOTALL)
                d = re.search(r"\[DETAIL\](.*?)$",                 proposal, re.DOTALL)
                if t and d:
                    _sb_write(lambda: supabase.table("dev_backlog").insert({
                        "title": f"[AI발의] {t.group(1).strip()}",
                        "task_detail": d.group(1).strip(),
                        "affected_file": "news_bot.py", "priority": 5, "status": "PENDING",
                    }).execute(), label="master_initiative")
                continue

            _sb_write(lambda: supabase.table("pending_approvals").insert({
                "agent_role": role, "proposed_instruction": proposal,
                "proposal_reason": f"{TODAY} 브리핑 기반 자율 발의",
                "needs_dev": False, "status": "PENDING",
            }).execute(), label="initiative")
            print(f"  ✅ [{role}] 등록 완료")

        except Exception as e:
            _record_error("INITIATIVE", role, e)

    print("🧠 [Initiative] 완료")


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
