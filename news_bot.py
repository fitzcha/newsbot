# 개선 제안 1: 뉴스 컨텍스트 제공 방식 개선

# Situation: 현재 뉴스 컨텍스트 제공 시, 관련 뉴스를 단순 나열하여 정보 파악 및 신뢰도 판단이 어려움.
# Behavior: 각 뉴스 기사별 핵심 내용 요약 및 긍/부정 감성 분석, 언론사 평판 등 출처 신뢰도 정보를 함께 제공.
# Impact: 사용자 정보 신뢰도 판단 및 맥락 파악 시간 단축, 정보 활용도 향상.

def improve_news_context(news_list):
    """뉴스 목록을 받아 핵심 내용 요약, 감성 분석, 신뢰도 정보를 추가하여 반환합니다."""
    # 1. (가정) 뉴스 기사 제목, 내용, 언론사 정보를 담은 news_list 를 입력 받음
    # 2. 각 기사별 요약 및 감성 분석 수행 (Gemini API 활용)
    # 3. 언론사 평판 점수 반영 (별도 DB 또는 API 활용)
    # 4. 최종 결과 반환
    
    updated_news_list = []
    for news in news_list:
        summary = call_agent(f"뉴스 요약: {news['title']} 내용: {news['content']}", agents['NEWS_SUMMARY'], force_one_line=True)
        sentiment = call_agent(f"뉴스 감성 분석: {news['title']} 내용: {news['content']}", agents['SENTIMENT_ANALYSIS'], force_one_line=True)
        trust_score = get_publisher_trust_score(news['publisher']) # 가정: 언론사 신뢰도 점수 반환 함수
        updated_news = {
            **news,
            "summary": summary,
            "sentiment": sentiment,
            "trust_score": trust_score
        }
        updated_news_list.append(updated_news)
    return updated_news_list

def get_publisher_trust_score(publisher):
    """언론사 이름을 받아 신뢰도 점수를 반환합니다 (가정)."""
    # DB 또는 API 연동하여 언론사 신뢰도 점수 반환 로직 구현
    # 예시: 신뢰도 점수는 0~100 사이의 값으로 표현
    if publisher == "연합뉴스":
        return 90
    elif publisher == "조선일보":
        return 60
    else:
        return 70

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

                # 개선: 뉴스 컨텍스트 제공 방식 개선 적용
                articles = []
                kw_ctx   = []
                
                # 기존 코드 주석 처리 또는 제거
                # for n in news_list:
                #     pm_summary = call_agent(f"뉴스: {n['title']}", agents['BRIEF'], force_one_line=True)
                #     impact     = call_agent(
                #         f"뉴스: {n['title']}\n전망 1줄.",
                #         agents.get('STOCK', agents['BRIEF']),
                #         force_one_line=True
                #     )
                #     articles.append({**n, "keyword": word, "pm_summary": pm_summary, "impact": impact})
                #     kw_ctx.append(n['title'])
                #     all_articles.append(f"[{word}] {n['title']}")
                
                updated_news_list = improve_news_context(news_list) # 개선된 함수 호출
                
                # 업데이트 된 뉴스 정보를 활용하여 articles 및 kw_ctx 생성
                for n in updated_news_list:
                    articles.append({**n, "keyword": word})
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