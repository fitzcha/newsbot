#!/usr/bin/env python3
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

_PROTECTED_ROLES = {"BRIEF", "HR", "MASTER", "DEV", "QA"}

# ──────────────────────────────────────────────
# 마크다운 완전 제거 유틸
# ──────────────────────────────────────────────
def strip_markdown(text: str) -> str:
    """
    Gemini 출력에서 유저에게 노출되면 안 되는 마크다운/레이블을 제거한다.
    - **굵게**, *기울임* 제거
    - **상황:**, **Situation:**, **BEHAVIOR:**, **IMPACT:**, **제안:** 등 레이블 줄 제거
    - 번호 목록(1. 2. 3.) → 내용만 유지
    - 불필요한 빈 줄 정리
    """
    # 볼드/이탤릭 마크다운 기호 제거 (* ** ***)
    text = re.sub(r'\*{1,3}', '', text)
    # 헤더(## 제목) 제거
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # 번호 목록 기호 제거 (1. 2. 등)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    # 글머리 기호 제거 (- * •)
    text = re.sub(r'^[\-\*•]\s+', '', text, flags=re.MULTILINE)

    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        # "레이블:" 패턴만으로 이루어진 줄 제거
        # 예: "상황:", "Situation:", "BEHAVIOR:", "IMPACT:", "제안:", "현황:"
        if re.match(r'^[A-Za-z가-힣\s·\-_]+:\s*$', stripped):
            continue
        # 빈 줄 연속 2개 이상 → 1개로
        if stripped == '' and clean and clean[-1] == '':
            continue
        clean.append(stripped)

    return '\n'.join(clean).strip()


def clean_role_name(s: str) -> str:
    """Gemini가 역할명에 붙인 ** 등 마크다운 제거"""
    return re.sub(r'\*+', '', s).strip()


# ──────────────────────────────────────────────
# 환경변수 체크
# ──────────────────────────────────────────────
def _check_env():
    missing = []
    critical_missing = []
    
    checks = [
        ("GEMINI_API_KEY",     GEMINI_KEY,  True),   # 치명적
        ("SUPABASE_URL",       SB_URL,      True),   # 치명적
        ("SUPABASE_KEY",       SB_KEY,      True),   # 치명적
        ("GMAIL_APP_PASSWORD", GMAIL_PASS,  False),  # 경고만
        ("YOUTUBE_API_KEY",    YOUTUBE_KEY, False),  # 경고만
    ]
    
    for key, val, is_critical in checks:
        if not val:
            missing.append(key)
            if is_critical:
                critical_missing.append(key)
    
    if critical_missing:
        error_msg = f"🚨 [ENV] 치명적 환경변수 누락: {', '.join(critical_missing)}"
        print(error_msg)
        print("❌ 시스템을 안전하게 종료합니다.")
        raise EnvironmentError(error_msg)
    
    if missing:
        print(f"⚠️  [ENV] 선택적 환경변수 누락 (기능 제한): {', '.join(missing)}")
    
    print("✅ [ENV] 필수 환경변수 확인 완료")

_check_env()

# ──────────────────────────────────────────────
# Gmail SMTP
# ──────────────────────────────────────────────
def _send_gmail(to, subject: str, html: str) -> bool:
    """
    Gmail SMTP로 이메일 발송
    
    Returns:
        bool: 발송 성공 여부
    """
    if not GMAIL_PASS:
        print("  ⚠️ [Email] GMAIL_APP_PASSWORD 미설정 — 메일 발송 스킵")
        return False
    
    recipients = [to] if isinstance(to, str) else to
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Fitz Intelligence <{GMAIL_USER}>"
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.sendmail(GMAIL_USER, recipients, msg.as_string())
        print(f"  ✅ [Email] 발송 성공: {recipients}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"  🚨 [Email] 인증 실패 (계정/비밀번호 확인 필요): {e}")
        return False
    except smtplib.SMTPException as e:
        print(f"  🚨 [Email] SMTP 오류: {e}")
        return False
    except Exception as e:
        print(f"  🚨 [Email] 발송 실패: {e}")
        return False

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
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오. 마크다운 볼드(**), 헤더(##), 번호목록(1.) 등 마크다운 문법을 절대 사용하지 마십시오.)"
    fp    = f"(경고: 반드시 '딱 1줄'로만 핵심을 작성하라. 마크다운 기호 절대 금지) {prompt}" if force_one_line else prompt + guard

    for attempt in range(3):
        try:
            res = google_genai.models.generate_content(
                model=_DEFAULT_MODEL,
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {fp}"
            )
            try:
                usage = res.usage_metadata
                record_cost(
                    call_type     = agent_info.get('agent_role', 'UNKNOWN'),
                    input_tokens  = getattr(usage, 'prompt_token_count',     0),
                    output_tokens = getattr(usage, 'candidates_token_count', 0),
                )
            except: pass

            output = strip_markdown(res.text.strip())
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
    guard = " (주의: 고객 리포트이므로 내부 학습 제안이나 '수정하겠습니다' 같은 말은 절대 포함하지 마십시오. JSON 값 안에도 마크다운 기호(**,##,*,- 등)를 절대 사용하지 마십시오.)"

    json_instruction = """

반드시 아래 JSON 형식으로만 응답하라. 마크다운, 코드블록, 설명 텍스트 일절 금지.
JSON 값 안에 **, *, ##, 번호목록(1. 2.) 등 마크다운 기호를 절대 사용하지 마라.
{
  "summary": "핵심 한 줄 요약 (40~60자, 마크다운 기호 없이 평문으로)",
  "points": ["포인트1 (1~2문장, 평문)", "포인트2 (1~2문장, 평문)", "포인트3 (1~2문장, 평문)"],
  "deep": ["심층분석1 (1~2문장, 평문)", "심층분석2", "심층분석3", "심층분석4"]
}
"""
    full_prompt = prompt + guard + json_instruction

    for attempt in range(3):
        try:
            res = google_genai.models.generate_content(
                model=_DEFAULT_MODEL,
                contents=f"당신은 {role}입니다.\n지침: {agent_info['instruction']}\n\n입력: {full_prompt}"
            )
            try:
                usage = res.usage_metadata
                record_cost(
                    call_type     = agent_info.get('agent_role', 'UNKNOWN'),
                    input_tokens  = getattr(usage, 'prompt_token_count',     0),
                    output_tokens = getattr(usage, 'candidates_token_count', 0),
                )
            except: pass

            raw = res.text.strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$",     "", raw)
            raw = raw.strip()
            brace_start = raw.find('{')
            brace_end   = raw.rfind('}')
            if brace_start != -1 and brace_end != -1:
                raw = raw[brace_start:brace_end + 1]
            parsed = json.loads(raw)

            # JSON 값 안에 남은 마크다운도 후처리로 제거
            parsed['summary'] = strip_markdown(str(parsed.get('summary', '')))
            parsed['points']  = [strip_markdown(str(p)) for p in parsed.get('points', [])]
            parsed['deep']    = [strip_markdown(str(d)) for d in parsed.get('deep', [])]
            return parsed

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
                return {"summary": strip_markdown(res.text.strip().split('\n')[0][:80]), "points": [], "deep": []}
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

_EXPERT_DOMAINS = [
    "kdi.re.kr", "nipa.kr", "iitp.kr", "kisdi.re.kr",
    "kotra.or.kr", "kiet.re.kr", "kiep.go.kr", "kistep.re.kr",
    "mckinsey.com", "bcg.com", "deloitte.com", "pwc.com",
    "gartner.com", "hbr.org", "mit.edu", "stanford.edu",
    "hankyung.com", "mk.co.kr", "sedaily.com",
    "zdnet.co.kr", "etnews.com", "techcrunch.com",
    "venturebeat.com", "bloomberg.com", "reuters.com", "ft.com",
    # 새로 추가
    "yozm.wishket.com", "brunch.co.kr", "theverge.com",

_NORMAL_DOMAINS = [
    "naver.com", "daum.net", "joins.com", "chosun.com",
    "donga.com", "hani.co.kr", "yonhapnews.co.kr",
]

def collect_expert_contents(word: str, agents: dict, max_per_domain: int = 2) -> list:
    """
    master.html의 agents.crawl_sites를 우선 사용하고, 
    부족하면 하드코딩 도메인으로 보충
    """
    print(f"  🎓 [{word}] 전문 콘텐츠 수집 시작...")
    brief_agent = agents.get('BRIEF')
    collected   = []
    seen_titles = set()
    
    # ═══ 1단계: DB에서 crawl_sites 로드 ═══
    db_domains = []
    try:
        agent_res = supabase.table("agents").select("agent_role, crawl_sites").execute()
        for a in (agent_res.data or []):
            sites = a.get("crawl_sites") or []
            for site in sites:
                if isinstance(site, dict) and site.get("policy") == "allow":
                    url = site.get("url", "")
                    if url:
                        domain = url.replace("https://", "").replace("http://", "").split("/")[0]
                        db_domains.append(domain)
        
        db_domains = list(dict.fromkeys(db_domains))
        if db_domains:
            print(f"    💾 [DB] master.html에서 등록된 사이트 {len(db_domains)}개 로드")
    except Exception as e:
        print(f"    ⚠️ [DB] crawl_sites 조회 실패 ({e}) — 하드코딩 사용")
    
    # ═══ 2단계: DB + Fallback 병합 ═══
    expert_domains = []
    if db_domains:
        expert_domains.extend(db_domains[:15])
    
    if len(expert_domains) < 5:
        needed = 10 - len(expert_domains)
        print(f"    🔄 [Fallback] DB 도메인 부족 — 하드코딩 {needed}개 보충")
        expert_domains.extend(_EXPERT_DOMAINS[:needed])
    
    # ═══ 3단계: 크롤링 함수 ═══
    def _scrape(domain: str, is_expert: bool):
        try:
            lang = _DOMAIN_LANG.get(domain, 'en')
            gn = GNews(language=lang, max_results=max_per_domain)
            news = gn.get_news(f"{word} site:{domain}") or []
            
            for n in news:
                title = (n.get("title") or "").strip()
                url = n.get("url") or n.get("link") or ""
                if not title or title in seen_titles or not url:
                    continue
                seen_titles.add(title)
                
                expert_summary = ""
                if brief_agent:
                    try:
                        raw = call_agent(
                            f"아래 제목의 핵심을 40자 이내 1줄로 요약. 마크다운 금지.\n제목: {title}",
                            brief_agent, force_one_line=True
                        )
                        expert_summary = strip_markdown(raw).split('\n')[0][:80]
                    except: pass
                
                collected.append({
                    "title": title,
                    "url": url,
                    "source_domain": domain,
                    "is_expert_content": is_expert,
                    "expert_summary": expert_summary,
                })
            
            if news:
                print(f"    📌 [Expert] [{domain}] '{word}' → {len(news)}건")
        except Exception as e:
            print(f"    ⚠️ [Expert] [{domain}] 실패: {e}")
    
    # ═══ 4단계: Expert 도메인 크롤링 ═══
    for domain in expert_domains:
        if len(collected) >= 10: break
        _scrape(domain, is_expert=True)
    
    # ═══ 5단계: 부족 시 일반 도메인 보충 ═══
    if len(collected) < 3:
        print(f"  📌 [Expert] 부족({len(collected)}건) — 일반 도메인 보충")
        for domain in _NORMAL_DOMAINS:
            if len(collected) >= 6: break
            _scrape(domain, is_expert=False)
    
    # ═══ 6단계: 정렬 및 결과 출력 ═══
    collected.sort(key=lambda x: (0 if x["is_expert_content"] else 1))
    expert_count = sum(1 for c in collected if c["is_expert_content"])
    normal_count = len(collected) - expert_count
    
    print(f"  ✅ [Expert] '{word}' → 총 {len(collected)}건 "
          f"(심층:{expert_count}건 / 일반:{normal_count}건)")
    return collected


# 다음 함수로 바로 이어짐 (설명 텍스트 없이)
def get_expert_with_cache(word: str, agents: dict) -> list:
    try:
        cache = supabase.table("expert_cache") \
            .select("contents").eq("keyword", word).eq("cache_date", TODAY).execute()
        if cache.data:
            print(f"  🎓 [Expert Cache] '{word}' → 캐시 재사용")
            return cache.data[0]["contents"]
    except Exception as e:
        print(f"  ⚠️ [Expert Cache] 조회 실패: {e}")

    contents = collect_expert_contents(word, agents)

    try:
        supabase.table("expert_cache").upsert({
            "keyword":    word,
            "cache_date": TODAY,
            "contents":   contents,
        }, on_conflict="keyword,cache_date").execute()
        print(f"  💾 [Expert Cache] '{word}' → 저장 완료")
    except Exception as e:
        print(f"  ⚠️ [Expert Cache] 저장 실패: {e}")

    return contents

def get_expert_with_cache(word: str, agents: dict) -> list:
    try:
        cache = supabase.table("expert_cache") \
            .select("contents").eq("keyword", word).eq("cache_date", TODAY).execute()
        if cache.data:
            print(f"  🎓 [Expert Cache] '{word}' → 캐시 재사용")
            return cache.data[0]["contents"]
    except Exception as e:
        print(f"  ⚠️ [Expert Cache] 조회 실패: {e}")

    contents = collect_expert_contents(word, agents)

    try:
        supabase.table("expert_cache").upsert({
            "keyword":    word,
            "cache_date": TODAY,
            "contents":   contents,
        }, on_conflict="keyword,cache_date").execute()
        print(f"  💾 [Expert Cache] '{word}' → 저장 완료")
    except Exception as e:
        print(f"  ⚠️ [Expert Cache] 저장 실패: {e}")

    return contents
    
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

def send_email_report(to_email: str, report: dict, yt_videos: list) -> bool:
    """
    이메일 리포트 발송 (재시도 로직 포함)
    
    Args:
        to_email: 수신자 이메일
        report: 리포트 데이터
        yt_videos: YouTube 영상 리스트
    
    Returns:
        bool: 발송 성공 여부
    """
    html = _build_email_html(report, yt_videos)
    subject = f"📊 {TODAY} Fitz Intelligence 일일 브리핑"
    
    # 최대 3회 재시도
    for attempt in range(3):
        try:
            success = _send_gmail(to_email, subject, html)
            
            if success:
                print(f"  ✅ [{to_email}] 이메일 발송 성공 (시도 {attempt + 1}/3)")
                return True
            else:
                if attempt < 2:
                    wait_time = 2 ** attempt  # 1초, 2초
                    print(f"  ⏳ [{to_email}] {wait_time}초 후 재시도...")
                    time.sleep(wait_time)
                else:
                    print(f"  ❌ [{to_email}] 이메일 발송 최종 실패")
                    return False
                    
        except Exception as e:
            print(f"  🚨 [{to_email}] 발송 중 예외 발생: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return False
    
    return False

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
    if os.path.basename(file_path) != "news_bot.py":
        return
    required = [
        "def run_autonomous_engine(",
        "def run_agent_initiative(",
        'if __name__ == "__main__":',
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
            _notify(f"검증 실패 — '{task['title']}' 롤백", err_detail, is_fail=True)
            supabase.table("dev_backlog").update({"status": "VALIDATION_ERROR"})\
                .eq("id", task['id']).execute()
            return

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_code)

        _run_cmd('git config --global user.name "Fitz-Dev"')
        _run_cmd('git config --global user.email "positivecha@gmail.com"')
        _run_cmd(f'git add {file_path}')
        task_title = task["title"][:60]
        _run_cmd(f'git commit -m "🤖 [DEV] {task_title}"')
        branch = os.environ.get("GITHUB_REF_NAME") or "main"
        _run_cmd(f"git push origin HEAD:{branch}")

        supabase.table("dev_backlog").update({"status": "DEPLOYED"})\
            .eq("id", task['id']).execute()
        print(f"  🚀 [DEV] 배포 완료: {task['title']}")
        _notify(f"배포 완료 — '{task['title']}'", f"작업이 성공적으로 배포되었습니다.\n{task['task_detail'][:200]}")

    except Exception as e:
        msg = f"작업: {task['title'] if task else '알 수 없음'}\n오류: {e}"
        print(f"🚨 [DEV] 처리 실패: {e}")
        _notify("DEV 처리 실패", msg, is_fail=True)
        if task:
            supabase.table("dev_backlog").update({"status": "DEPLOY_FAILED"})\
                .eq("id", task['id']).execute()
        if cur_code and file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(cur_code)
                print(f"  ↩️ [DEV] 원본 코드로 롤백 완료")
            except: pass

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
# [4] 이메일 발송 — 뉴스레터 템플릿 (전문 콘텐츠 포함)
# ──────────────────────────────────────────────
def _build_email_html(report, yt_videos=None):
    bk        = report.get("by_keyword", {})
    yt_videos = yt_videos or []

    keyword_sections = ""
    kw_list = list(bk.items())

    for idx, (kw, kd) in enumerate(kw_list):
        articles = kd.get("articles", [])
        expert_contents = kd.get("expert_contents", [])[:3]  # ← 추가
        ba_brief = kd.get("ba_brief", {})

        # 기사 섹션
        article_rows = ""
        for a in articles[:3]:
            title      = a.get("title", "")
            pm_summary = strip_markdown(a.get("pm_summary", ""))
            url        = a.get("url", a.get("link", "#"))
            article_rows += f"""
              <tr>
                <td style="padding:10px 0; border-bottom:1px solid #f0f0f0;">
                  <p style="margin:0 0 4px 0; font-size:14px; font-weight:600; color:#1a1a1a; line-height:1.4;">{title}</p>
                  <p style="margin:0 0 6px 0; font-size:13px; color:#666; line-height:1.5;">{pm_summary}</p>
                  <a href="{url}" style="font-size:12px; color:#2563eb; font-weight:700; text-decoration:none;">더 자세히 알아보기 →</a>
                </td>
              </tr>"""

        # 전문 콘텐츠 섹션 (새로 추가)
        expert_rows = ""
        if expert_contents:
            expert_rows = """
            <tr><td style="padding-top:16px;">
              <div style="font-size:11px;font-weight:700;color:#7c3aed;letter-spacing:1px;margin-bottom:10px;">🎓 EXPERT INSIGHTS</div>
            </td></tr>"""
            
            for exp in expert_contents:
                exp_title = exp.get("title", "")
                exp_url = exp.get("url", "#")
                exp_summary = exp.get("expert_summary", "")
                exp_source = exp.get("source_domain", "")
                
                expert_rows += f"""
                <tr><td style="padding:10px 0; border-bottom:1px solid #f3e8ff;">
                  <a href="{exp_url}" style="color:#7c3aed;font-weight:600;font-size:14px;text-decoration:none;line-height:1.4;">{exp_title}</a>
                  <div style="font-size:11px;color:#94a3b8;margin-top:3px;">{exp_source}</div>
                  <div style="font-size:13px;color:#64748b;margin-top:6px;line-height:1.5;">{exp_summary}</div>
                </td></tr>"""

        # BA 브리핑 섹션
        if isinstance(ba_brief, dict):
            ba_items = []
            if ba_brief.get("summary"):
                ba_items.append(strip_markdown(ba_brief["summary"]))
            ba_items += [strip_markdown(p) for p in ba_brief.get("points", [])]
        else:
            ba_items = [strip_markdown(l.strip()) for l in str(ba_brief).split('\n') if l.strip()][:5]

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
          {expert_rows and f'<tr><td><table width="100%" cellpadding="0" cellspacing="0">{expert_rows}</table></td></tr>' or ''}
          {ba_html and f'<tr><td style="padding-top:14px;"><ul style="margin:0; padding-left:18px;">{ba_html}</ul></td></tr>' or ''}
        </table>
        {divider}"""

    yt_block        = build_youtube_email_block(yt_videos)
    dashboard_block = f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a; border-radius:10px; margin-top:32px;">
          <tr>
            <td style="padding:28px 32px; text-align:center;">
              <p style="margin:0 0 16px 0; font-size:18px; font-weight:700; color:#fff;">오늘의 전체 인사이트 확인하기</p>
              <a href="{DASHBOARD_URL}" style="display:inline-block; background:#e8472a; color:#fff; font-size:14px; font-weight:700; padding:14px 32px; border-radius:10px; text-decoration:none;">대시보드 바로가기 →</a>
            </td>
          </tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Fitz 비즈니스 인사이트 리포트</title>
</head>
<body style="margin:0; padding:0; background-color:#f9fafb; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb; padding:40px 20px;">
    <tr>
      <td align="center">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px; background:#fff; border-radius:16px; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
          <tr>
            <td style="padding:32px 32px 24px 32px;">
              <h1 style="margin:0 0 8px 0; font-size:26px; font-weight:800; color:#111; letter-spacing:-0.5px;">📊 오늘의 비즈니스 인사이트</h1>
              <p style="margin:0; font-size:14px; color:#94a3b8;">{TODAY}</p>
            </td>
          </tr>
          <tr><td style="padding:0 32px 32px 32px;">{keyword_sections}</td></tr>
          {yt_block and f'<tr><td style="padding:0 32px 32px 32px;">{yt_block}</td></tr>' or ''}
          <tr><td style="padding:0 32px 32px 32px;">{dashboard_block}</td></tr>
          <tr>
            <td style="padding:24px 32px; background:#f8fafc; border-top:1px solid #e2e8f0; text-align:center;">
              <p style="margin:0; font-size:12px; color:#94a3b8;">
                © 2025 Fitz Intelligence. All rights reserved.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

# ──────────────────────────────────────────────
# [BRIEF 역할 ①] 직원 수집 소스 지시 + 실제 크롤링
# ──────────────────────────────────────────────
_DOMAIN_LANG = {
    "reuters.com": "en", "bloomberg.com": "en", "ft.com": "en",
    "techcrunch.com": "en", "wsj.com": "en", "cnbc.com": "en",
    "naver.com": "ko", "naver_finance": "ko", "hankyung.com": "ko",
    "mk.co.kr": "ko", "chosun.com": "ko", "joins.com": "ko",
    "zdnet.co.kr": "ko", "platum.kr": "ko", "venturebeat.com": "en",
    "investing.com": "en", "seekingalpha.com": "en",
    "jobplanet.co.kr": "ko", "linkedin.com": "en",
    # 새로 추가
    "yozm.wishket.com": "ko",
    "brunch.co.kr": "ko",
    "theverge.com": "en",
}

def brief_get_source_directive(word: str, agents: dict) -> dict:
    brief_agent = agents.get('BRIEF')
    if not brief_agent:
        return {}

    prompt = (
        f"키워드: '{word}'\n\n"
        "당신은 분석팀 리더(BRIEF)입니다. "
        "오늘 이 키워드와 관련해 각 담당자(BA, STOCK, PM, HR)가 "
        "어떤 사이트나 소스에서 콘텐츠를 집중 수집해야 하는지 지시하십시오.\n\n"
        "반드시 아래 JSON 형식으로만 응답하라. 설명·마크다운 금지.\n"
        "사이트명은 도메인 형식(예: reuters.com, hankyung.com)으로 작성.\n"
        "{\n"
        '  "BA":    ["사이트1", "사이트2"],\n'
        '  "STOCK": ["사이트1", "사이트2"],\n'
        '  "PM":    ["사이트1", "사이트2"],\n'
        '  "HR":    ["사이트1", "사이트2"]\n'
        "}"
    )

    raw = call_agent(prompt, brief_agent, force_one_line=False)
    try:
        raw_clean = re.sub(r"```[a-z]*|```", "", raw).strip()
        brace_s = raw_clean.find('{')
        brace_e = raw_clean.rfind('}')
        if brace_s != -1 and brace_e != -1:
            raw_clean = raw_clean[brace_s:brace_e+1]
        directive = json.loads(raw_clean)
        print(f"  📋 [BRIEF→직원] '{word}' 수집 소스 지시 완료: {directive}")
        return directive
    except Exception as e:
        print(f"  ⚠️ [BRIEF→직원] 파싱 실패 ({e}) — 기본 소스 사용")
        return {}


def collect_news_by_directive(word: str, directive: dict) -> list:
    all_sources = []
    for role_sources in directive.values():
        all_sources.extend(role_sources)
    unique_sources = list(dict.fromkeys(all_sources))

    if not unique_sources:
        is_korean = any(ord(c) > 0x1100 for c in word)
        gn = GNews(language='ko' if is_korean else 'en', max_results=10)
        return gn.get_news(word) or []

    collected = []
    seen_titles = set()

    for domain in unique_sources:
        try:
            lang = _DOMAIN_LANG.get(domain, None)
            if lang is None:
                lang = 'ko' if any(ord(c) > 0x1100 for c in domain) else 'en'

            site_query = f"{word} site:{domain}" if '.' in domain else word
            gn = GNews(language=lang, max_results=3)
            news = gn.get_news(site_query) or []

            for n in news:
                title = n.get("title", "")
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    n['source_domain'] = domain
                    collected.append(n)

            if news:
                print(f"    📌 [{domain}] '{word}' → {len(news)}건 수집")

        except Exception as e:
            print(f"    ⚠️ [{domain}] 수집 실패: {e}")
            continue

    if len(collected) < 5:
        try:
            is_korean = any(ord(c) > 0x1100 for c in word)
            gn = GNews(language='ko' if is_korean else 'en', max_results=10)
            fallback = gn.get_news(word) or []
            for n in fallback:
                title = n.get("title", "")
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    n['source_domain'] = 'gnews_fallback'
                    collected.append(n)
            print(f"    🔄 [GNews 보충] '{word}' → {len(fallback)}건 추가")
        except Exception as e:
            print(f"    ⚠️ [GNews 보충] 실패: {e}")

    print(f"  📰 [BRIEF 지시 수집] '{word}' 총 {len(collected)}건 (소스: {unique_sources})")
    return collected


# ──────────────────────────────────────────────
# [BRIEF 역할 ②] 전문 콘텐츠 크롤링
# ──────────────────────────────────────────────
def collect_expert_contents(word: str, directive: dict) -> list:
    """
    Brief가 지시한 소스에서 전문 콘텐츠를 크롤링한다.
    뉴스와 달리 심층 분석, 리포트, 블로그 등을 수집한다.
    """
    # 모든 역할의 소스를 합쳐서 상위 3개 사이트 선정
    all_sources = []
    for role_sources in directive.values():
        all_sources.extend(role_sources)
    
    unique_sources = list(dict.fromkeys(all_sources))[:3]  # 상위 3개만
    
    if not unique_sources:
        print(f"  ℹ️ [Expert] '{word}' 지정된 소스 없음")
        return []
    
    collected = []
    seen_titles = set()
    
    for domain in unique_sources:
        try:
            # 전문 콘텐츠는 일반 뉴스보다 깊이 있는 키워드 조합 사용
            search_queries = [
                f"{word} analysis site:{domain}",
                f"{word} report site:{domain}",
                f"{word} insight site:{domain}",
            ]
            
            lang = _DOMAIN_LANG.get(domain, 'en' if '.' in domain else 'ko')
            
            for query in search_queries:
                try:
                    gn = GNews(language=lang, max_results=2)
                    results = gn.get_news(query) or []
                    
                    for item in results:
                        title = item.get("title", "")
                        url = item.get("url", "")
                        
                        # 중복 제거 및 품질 필터
                        if title and title not in seen_titles and len(title) > 20:
                            seen_titles.add(title)
                            
                            # 전문 콘텐츠 점수 계산 (제목 기반 휴리스틱)
                            is_expert = any(keyword in title.lower() for keyword in [
                                'analysis', 'report', 'insight', 'research', 'study',
                                '분석', '리포트', '보고서', '연구', '심층'
                            ])
                            
                            collected.append({
                                **item,
                                'source_domain': domain,
                                'is_expert_content': is_expert,
                                'keyword': word,
                            })
                    
                    if results:
                        print(f"    🎓 [{domain}] '{query}' → {len(results)}건 수집")
                        time.sleep(1)  # Rate limiting
                        
                except Exception as e:
                    print(f"    ⚠️ [{domain}] '{query}' 수집 실패: {e}")
                    continue
                    
        except Exception as e:
            print(f"    ⚠️ [{domain}] 전체 수집 실패: {e}")
            continue
    
    print(f"  🎓 [Expert Contents] '{word}' 총 {len(collected)}건 (소스: {unique_sources})")
    return collected

# ──────────────────────────────────────────────
# [5] 자율 분석 엔진
# ──────────────────────────────────────────────
def run_autonomous_engine():
    agents = get_agents()
    print(f"🚀 {TODAY} Sovereign Engine v18.0 가동")

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
                print(f"  📋 [{word}] BRIEF 수집 소스 지시 중...")
                source_directive = brief_get_source_directive(word, agents)
                ba_src  = source_directive.get('BA',    [])
                pm_src  = source_directive.get('PM',    [])
                stk_src = source_directive.get('STOCK', [])

                print(f"  📰 [{word}] BRIEF 지시 소스 기반 뉴스 수집 중...")
                news_list = collect_news_by_directive(word, source_directive)

                record_performance(user_id, word, len(news_list))

                if not news_list:
                    print(f"  ⚠️  [{word}] 뉴스 없음 — 스킵")
                    by_keyword[word] = {
                        "ba_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "securities_brief": {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "pm_brief":         {"summary": "해당 키워드의 뉴스를 찾을 수 없습니다.", "points": [], "deep": []},
                        "articles":         [],
                        "youtube_videos":   [],
                        "expert_contents":  [],
                        "source_directive": source_directive,
                    }
                    continue

                articles = []
                kw_ctx   = []
                for n in news_list:
                    # pm_summary: 1줄 요약 후 마크다운 제거
                    pm_summary_raw = call_agent(
                        f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True
                    )
                    pm_summary = strip_markdown(pm_summary_raw).split('\n')[0]

                    impact_raw = call_agent(
                        f"뉴스: {n['title']}\n전망 1줄.",
                        agents.get('STOCK', agents['BRIEF']),
                        force_one_line=True
                    )
                    impact = strip_markdown(impact_raw).split('\n')[0]

                    articles.append({**n, "keyword": word, "pm_summary": pm_summary, "impact": impact})
                    kw_ctx.append(n['title'])
                    all_articles.append(f"[{word}] {n['title']}")

                print(f"  🎬 [{word}] YouTube 수집 중...")
                yt_videos = get_youtube_with_cache(word)
                all_yt.extend(yt_videos)
                yt_ctx = build_youtube_context(yt_videos)
                print(f"  🎓 [{word}] 전문 콘텐츠 수집 중...")
                expert_contents = get_expert_with_cache(word, agents)
                
                # ===== 전문 콘텐츠 수집 (추가) =====
                print(f"  🎓 [{word}] 전문 콘텐츠 수집 중...")
                expert_contents = collect_expert_contents(word, source_directive)
                
                # 전문 콘텐츠 요약 생성
                expert_summaries = []
                for content in expert_contents[:3]:  # 상위 3개만
                    try:
                        summary_raw = call_agent(
                            f"전문 콘텐츠: {content['title']}\n핵심 인사이트 1줄로 요약",
                            agents['BRIEF'],
                            force_one_line=True
                        )
                        summary = strip_markdown(summary_raw).split('\n')[0]
                        content['expert_summary'] = summary
                        expert_summaries.append(content)
                        time.sleep(1)  # Rate limiting
                    except Exception as e:
                        print(f"    ⚠️ 전문 콘텐츠 요약 실패: {e}")
                        content['expert_summary'] = content.get('description', '')[:100]
                        expert_summaries.append(content)

                # 컨텍스트 구성
                ctx = "\n".join(kw_ctx)
                if yt_ctx:
                    ctx += f"\n\n{yt_ctx}"

                hint_ba  = f"\n\n[BRIEF 지시 — 오늘 중점 참고 소스: {', '.join(ba_src)}]"  if ba_src  else ""
                hint_pm  = f"\n\n[BRIEF 지시 — 오늘 중점 참고 소스: {', '.join(pm_src)}]"  if pm_src  else ""
                hint_stk = f"\n\n[BRIEF 지시 — 오늘 중점 참고 소스: {', '.join(stk_src)}]" if stk_src else ""

                print(f"  🤖 [{word}] 에이전트 분석 중...")
                by_keyword[word] = {
                    "ba_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 비즈니스 수익 구조 및 경쟁 분석:\n{ctx}{hint_ba}",
                        agents['BA']
                    ),
                    "securities_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 주식 시장 반응 및 투자 인사이트:\n{ctx}{hint_stk}",
                        agents['STOCK']
                    ),
                    "pm_brief": call_agent_json(
                        f"키워드 '{word}' 뉴스 및 유튜브 기반 전략적 서비스 기획 브리핑:\n{ctx}{hint_pm}",
                        agents['PM']
                    ),
                    "articles":         articles,
                    "youtube_videos":   yt_videos,
                    "expert_contents":  expert_summaries,  # ← 추가
                    "source_directive": source_directive,
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
                
                # 이메일 발송 및 성공 여부 확인
                email_success = send_email_report(user_email, final_report, all_yt)
                
                # email_sent 플래그 업데이트 (재시도 3회)
                for retry in range(3):
                    try:
                        supabase.table("reports").update({"email_sent": email_success})\
                            .eq("id", report_id).execute()
                        print(f"  ✅ [DB] email_sent={email_success} 업데이트 완료")
                        break
                    except Exception as e:
                        if retry < 2:
                            print(f"  ⏳ [DB] email_sent 업데이트 재시도 ({retry + 1}/3)...")
                            time.sleep(1)
                        else:
                            print(f"  🚨 [DB] email_sent 업데이트 최종 실패: {e}")
                            # 최종 실패 시 관리자에게 알림
                            try:
                                _send_gmail(
                                    to="positivecha@gmail.com",
                                    subject="🚨 [시스템] email_sent 업데이트 실패",
                                    html=f"<pre>report_id: {report_id}\nuser: {user_email}\nerror: {e}</pre>"
                                )
                            except:
                                pass
                
                status_msg = "이메일 발송 완료" if email_success else "이메일 발송 실패 (DB에 기록됨)"
                print(f"✅ [{user_email}] 리포트 저장 완료 (YouTube {len(all_yt)}개 포함) — {status_msg}")

        except Exception as e:
            print(f"❌ 유저 에러 ({user.get('email','?')}): {e}")
            continue

    record_supabase_stats()
    sync_data_to_github()
    run_agent_initiative(by_keyword_all=_collect_all_by_keyword(user_res.data or []))


def _collect_all_by_keyword(users: list) -> dict:
    """모든 유저의 by_keyword 데이터를 병합"""
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
    try:
        ind_map = supabase.table("industry_map")\
            .select("industry, keywords")\
            .eq("is_active", True).execute()
    except Exception as e:
        print(f"  ⚠️ [Industry] industry_map 조회 실패: {e}")
        return

    agents = get_agents()

    for row in (ind_map.data or []):
        industry = row.get("industry", "")
        kws      = row.get("keywords", [])
        if not industry or not kws:
            continue

        all_articles = []
        for kw in kws[:3]:
            try:
                is_korean = any(ord(c) > 0x1100 for c in kw)
                gn        = GNews(language='ko' if is_korean else 'en', max_results=3)
                news      = gn.get_news(kw)
                all_articles.extend([{"title": n["title"], "keyword": kw} for n in news])
            except Exception as e:
                print(f"  ⚠️ [Industry] '{kw}' 수집 실패: {e}")

        if not all_articles:
            continue

        ctx = "\n".join([f"[{a['keyword']}] {a['title']}" for a in all_articles])
        try:
            summary = call_agent(
                f"산업군 '{industry}' 오늘 뉴스 동향 3줄 요약:\n{ctx}",
                agents.get("BA", agents.get("BRIEF")),
                force_one_line=False
            )
        except:
            summary = "요약 생성 실패"

        try:
            supabase.table("industry_monitor").upsert({
                "industry":     industry,
                "category":     industry,   # NOT NULL 제약 — industry 값으로 채움
                "articles":     all_articles,
                "summary":      summary,
                "monitor_date": TODAY,
            }, on_conflict="industry,monitor_date").execute()
            print(f"  ✅ [Industry] '{industry}' 동향 저장 완료 ({len(all_articles)}건)")
        except Exception as e:
            print(f"  ❌ [Industry] '{industry}' 저장 실패: {e}")

    print("🏭 [Industry] 산업군 모니터링 완료")

# ──────────────────────────────────────────────
# [BRIEF 역할 ③] BRIEF→HR 에이전트 조직 파이프라인
# ──────────────────────────────────────────────
def run_brief_hr_org_pipeline(agents: dict, today_ctx: str, industry_ctx: str):
    brief_agent = agents.get('BRIEF')
    hr_agent    = agents.get('HR')
    if not brief_agent or not hr_agent:
        print("  ⚠️ [BRIEF→HR] BRIEF 또는 HR 에이전트 없음 — 파이프라인 스킵")
        return

    try:
        agent_res     = supabase.table("agents").select("agent_role").execute()
        current_roles = [a['agent_role'] for a in (agent_res.data or [])]
        current_roles_str = ", ".join(current_roles) if current_roles else "없음"
    except Exception as e:
        print(f"  ⚠️ [BRIEF→HR] 에이전트 목록 조회 실패: {e}")
        return

    print("  🧠 [BRIEF] 에이전트 조직 구성 제안 생성 중...")

    brief_prompt = (
        f"오늘 뉴스 컨텍스트:\n{today_ctx}\n\n"
        f"산업군 동향:\n{industry_ctx}\n\n"
        f"현재 가동 중인 에이전트: {current_roles_str}\n\n"
        "당신은 분석팀 리더(BRIEF)입니다. "
        "오늘 뉴스와 산업 동향을 분석해, 현재 팀에서 부족하거나 새로 필요한 전문가 역할을 제안하고, "
        "성과가 낮거나 중복되는 역할은 제거를 제안하십시오.\n\n"
        "반드시 아래 형식으로만 응답하라. 마크다운 기호(**,*,## 등) 절대 사용 금지:\n"
        "[ADD_AGENT]역할명1:역할설명1|역할명2:역할설명2\n"
        "[REMOVE_AGENT]역할명1:제거이유1|역할명2:제거이유2\n"
        "[REASON]전체 판단 근거를 2~3줄로 설명\n\n"
        "추가/제거가 필요 없으면 해당 태그 뒤에 '없음'이라고 적을 것.\n"
        f"절대로 {', '.join(_PROTECTED_ROLES)} 역할은 제거 제안하지 말 것."
    )

    brief_proposal = call_agent(brief_prompt, brief_agent, force_one_line=False)

    if not brief_proposal or brief_proposal in ["분석 지연 중", "분석 데이터 없음"]:
        print("  ⚠️ [BRIEF] 에이전트 조직 제안 없음 — 스킵")
        return

    print(f"  ✅ [BRIEF] 조직 제안 완료")

    print("  👤 [HR] BRIEF 제안 심사 중...")
    hr_prompt = (
        f"BRIEF 리더의 에이전트 조직 개편 제안:\n{brief_proposal}\n\n"
        f"현재 가동 중인 에이전트: {current_roles_str}\n"
        f"오늘 뉴스 컨텍스트:\n{today_ctx}\n\n"
        "당신은 HR 책임자입니다. "
        "BRIEF의 제안을 항목별로 심사하여 타당한 것은 승인, 부적절한 것은 거부하십시오.\n\n"
        "반드시 아래 형식으로만 응답하라. 마크다운 기호(**,*,## 등) 절대 사용 금지:\n"
        "[APPROVED_ADD]역할명1:역할설명1|역할명2:역할설명2  (없으면 '없음')\n"
        "[APPROVED_REMOVE]역할명1:제거이유1  (없으면 '없음')\n"
        "[REJECTED]거부 항목과 거부 이유\n"
        "[HR_COMMENT]최종 심사 의견 1~2줄"
    )

    hr_decision = call_agent(hr_prompt, hr_agent, force_one_line=False)

    if not hr_decision or hr_decision in ["분석 지연 중", "분석 데이터 없음"]:
        print("  ⚠️ [HR] 심사 결과 없음 — 스킵")
        return

    print(f"  ✅ [HR] 심사 완료")

    add_m     = re.search(r"\[APPROVED_ADD\](.*?)(?=\[APPROVED_REMOVE\]|\[REJECTED\]|\[HR_COMMENT\]|$)",  hr_decision, re.DOTALL)
    remove_m  = re.search(r"\[APPROVED_REMOVE\](.*?)(?=\[APPROVED_ADD\]|\[REJECTED\]|\[HR_COMMENT\]|$)", hr_decision, re.DOTALL)
    comment_m = re.search(r"\[HR_COMMENT\](.*?)$", hr_decision, re.DOTALL)

    add_raw    = (add_m.group(1).strip()     if add_m     else "").strip()
    remove_raw = (remove_m.group(1).strip()  if remove_m  else "").strip()
    hr_comment = (comment_m.group(1).strip() if comment_m else "HR 심사 완료").strip()

    approved_adds    = []
    approved_removes = []

    if add_raw and add_raw != "없음":
        for item in add_raw.split("|"):
            parts = item.strip().split(":", 1)
            if len(parts) == 2:
                approved_adds.append((clean_role_name(parts[0]), parts[1].strip()))

    if remove_raw and remove_raw != "없음":
        for item in remove_raw.split("|"):
            parts = item.strip().split(":", 1)
            if len(parts) == 2:
                approved_removes.append((clean_role_name(parts[0]), parts[1].strip()))

    for role_name, role_desc in approved_adds:
        if role_name in current_roles:
            print(f"  ⏭️  [BRIEF→HR] '{role_name}' 이미 존재 — 스킵")
            continue
        try:
            content = (
                f"[신규 에이전트 추가 제안]\n"
                f"역할명: {role_name}\n"
                f"역할 설명: {role_desc}\n\n"
                f"[BRIEF 원본 제안]\n{brief_proposal}\n\n"
                f"[HR 심사 의견]\n{hr_comment}"
            )
            supabase.table("pending_approvals").insert({
                "agent_role":           role_name,
                "proposed_instruction": content,
                "proposal_reason":      f"{TODAY} BRIEF 제안 → HR 승인 — 신규 에이전트 추가",
                "needs_dev":            False,
                "status":               "PENDING",
            }).execute()
            print(f"  ✅ [BRIEF→HR] 신규 에이전트 '{role_name}' pending_approvals 등록 완료")
        except Exception as e:
            print(f"  ❌ [BRIEF→HR] '{role_name}' 등록 실패: {e}")

    for role_name, remove_reason in approved_removes:
        if role_name in _PROTECTED_ROLES:
            print(f"  🛡️  [BRIEF→HR] '{role_name}'은 보호 역할 — 제거 불가")
            continue
        if role_name not in current_roles:
            print(f"  ⏭️  [BRIEF→HR] '{role_name}' 존재하지 않음 — 스킵")
            continue
        try:
            content = (
                f"[에이전트 제거 제안]\n"
                f"역할명: {role_name}\n"
                f"제거 이유: {remove_reason}\n\n"
                f"[BRIEF 원본 제안]\n{brief_proposal}\n\n"
                f"[HR 심사 의견]\n{hr_comment}"
            )
            supabase.table("pending_approvals").insert({
                "agent_role":           role_name,
                "proposed_instruction": content,
                "proposal_reason":      f"{TODAY} BRIEF 제안 → HR 승인 — 에이전트 제거",
                "needs_dev":            False,
                "status":               "PENDING",
            }).execute()
            print(f"  ✅ [BRIEF→HR] 에이전트 제거 제안 '{role_name}' pending_approvals 등록 완료")
        except Exception as e:
            print(f"  ❌ [BRIEF→HR] '{role_name}' 제거 제안 등록 실패: {e}")

    if not approved_adds and not approved_removes:
        print(f"  ℹ️  [BRIEF→HR] 승인된 변경 없음. HR 의견: {hr_comment}")

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
            "반드시 아래 형식으로만 응답하라. 마크다운 기호 절대 금지:\n"
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
            "점수와 근거를 반드시 포함할 것. 마크다운 기호(**,## 등) 절대 사용 금지."
        ),
        "DATA": (
            f"오늘 뉴스 수집 성과:\n{perf_ctx}\n\n"
            "뉴스 수집량이 적은 키워드나 품질 이슈를 분석하고 "
            "데이터 수집 전략 개선안을 instruction 업데이트 형태로 제안하라. 마크다운 기호 절대 사용 금지."
        ),
        "BA": (
            f"오늘 분석 컨텍스트:\n{today_ctx}\n\n"
            "오늘 비즈니스 분석에서 부족했던 점을 파악하고 "
            "더 날카로운 인사이트를 제공하기 위한 instruction 개선안을 제안하라. 마크다운 기호 절대 사용 금지."
        ),
        "BRIEF": (
            f"오늘 뉴스 컨텍스트:\n{today_ctx}\n\n"
            f"산업군 동향:\n{industry_ctx}\n\n"
            "당신은 분석팀 리더(BRIEF)입니다. "
            "오늘 전체 분석 품질을 리더 시각으로 자체 평가하고, "
            "BA·STOCK·PM·HR 각 담당자에게 내일 분석 개선을 위한 지시 사항을 제안하라.\n"
            "형식: [ROLE]역할명 [DIRECTIVE]지시내용 (각 역할마다 한 줄). 마크다운 기호 절대 사용 금지."
        ),
        "MASTER": (
            f"오늘 전체 시스템 성과:\n키워드 성과:\n{perf_ctx}\n\n뉴스 컨텍스트:\n{today_ctx}\n\n"
            "전체 에이전트 시스템의 오늘 성과를 종합 평가하고, "
            "가장 시급한 개발 또는 개선 안건 1가지를 dev_backlog 등록 형태로 제안하라. "
            "제안 형식: [TITLE]안건제목 [DETAIL]상세요구사항. 마크다운 기호(**,## 등) 절대 사용 금지."
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
                        "proposed_instruction": strip_markdown(proposal),
                        "proposal_reason":      f"{TODAY} KW 자율 발의 (파싱 실패)",
                        "needs_dev":            False,
                        "status":               "PENDING",
                    }).execute()
                    continue

                structured = (
                    f"[키워드 관리 제안]\n"
                    f"추가 추천: {', '.join(add_kws) if add_kws else '없음'}\n"
                    f"제거 추천: {', '.join(remove_kws) if remove_kws else '없음'}\n\n"
                    f"[근거]\n{strip_markdown(reason)}"
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
                    title  = strip_markdown(t.group(1).strip()).split('\n')[0]
                    detail = strip_markdown(d.group(1).strip())
                    supabase.table("dev_backlog").insert({
                        "title":         f"[AI발의] {title}",
                        "task_detail":   detail,
                        "affected_file": "news_bot.py",
                        "priority":      5,
                        "status":        "PENDING",
                    }).execute()
                    print(f"  📋 [MASTER] dev_backlog 자동 등록: {title}")
                continue

            if role == "BRIEF":
                supabase.table("pending_approvals").insert({
                    "agent_role":           "BRIEF",
                    "proposed_instruction": strip_markdown(proposal),
                    "proposal_reason":      f"{TODAY} BRIEF 리더 자율 발의 — 직원 지시 사항",
                    "needs_dev":            False,
                    "status":               "PENDING",
                }).execute()
                print(f"  ✅ [BRIEF] 자율 발의 등록 완료")
                continue

            supabase.table("pending_approvals").insert({
                "agent_role":           role,
                "proposed_instruction": strip_markdown(proposal),
                "proposal_reason":      f"{TODAY} 브리핑 데이터 기반 자율 발의",
                "needs_dev":            False,
                "status":               "PENDING",
            }).execute()
            print(f"  ✅ [{role}] 자율 발의 등록 완료 → HQ 결재 대기")

        except Exception as e:
            print(f"  ❌ [{role}] 자율 발의 실패: {e}")

    print("\n🏢 [BRIEF→HR] 에이전트 조직 구성 파이프라인 시작...")
    try:
        run_brief_hr_org_pipeline(agents, today_ctx, industry_ctx)
    except Exception as e:
        print(f"  ❌ [BRIEF→HR] 파이프라인 실패: {e}")
    print("🏢 [BRIEF→HR] 파이프라인 완료\n")

    print("🧠 [Initiative] 자율 발의 완료 — HQ에서 확인하세요")

# ──────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="Fitz News Bot - Sovereign Intelligence System")
    parser.add_argument('--mode', type=str, default='',
                        help='실행 모드: dev, BRIEFING, INDUSTRY, GOVERNANCE')
    parser.add_argument('--backlog-id', type=str, default='',
                        help='개발 백로그 ID (--mode dev 사용 시)')
    
    args = parser.parse_args()
    
    # 명령줄 인자로 모드 지정된 경우
    if args.mode:
        mode = args.mode.upper()
        
        if mode == 'DEV':
            # DEV 배포 모드: 특정 백로그 ID 처리
            backlog_id = args.backlog_id or os.environ.get("BACKLOG_ID", "")
            if not backlog_id:
                print("❌ [DEV] backlog_id가 지정되지 않았습니다")
                sys.exit(1)
            
            print(f"🛠️ [DEV] 개발 배포 모드 실행: backlog_id={backlog_id}")
            run_self_evolution(backlog_id)
            sys.exit(0)
            
        elif mode == 'GOVERNANCE':
            print("🌙 [GOVERNANCE] 23:30 마감 작업 모드")
            manage_deadline_approvals()
            sys.exit(0)
            
        elif mode == 'INDUSTRY':
            print("🏭 [INDUSTRY] 06:00 산업군 모니터링 모드")
            run_industry_monitor()
            sys.exit(0)
            
        elif mode == 'BRIEFING':
            print("☀️ [BRIEFING] 09:00 정기 브리핑 모드")
            manage_deadline_approvals()
            run_autonomous_engine()
            sync_data_to_github()
            sys.exit(0)
        else:
            print(f"⚠️ 알 수 없는 모드: {mode}")
            sys.exit(1)
    
    # 환경 변수로 모드 지정 (기존 방식 호환)
    cron_type = os.environ.get("CRON_TYPE", "BRIEFING").upper()
    
    if cron_type == "GOVERNANCE":
        print("🌙 [GOVERNANCE] 23:30 마감 작업 모드")
        manage_deadline_approvals()
    elif cron_type == "INDUSTRY":
        print("🏭 [INDUSTRY] 06:00 산업군 모니터링 모드")
        run_industry_monitor()
    else:
        print("☀️ [BRIEFING] 09:00 정기 브리핑 모드")
        manage_deadline_approvals()
        run_autonomous_engine()
        sync_data_to_github()
