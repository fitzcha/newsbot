#!/usr/bin/env python3
"""
Minimal end-to-end smoke gate for critical onboarding flow.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("SMOKE_PORT", "4173"))
BASE_URL = f"http://127.0.0.1:{PORT}"


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return


def start_static_server():
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), QuietStaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── supabase 스텁 (인증 여부만 다름, 두 버전 완전 독립) ──────────────────────

STUB_UNAUTH = """\
(function() {
  function ok(d) { return Promise.resolve({ data: d, error: null }); }
  function bq(t) {
    var q = {
      select: function() { return q; },
      eq:     function() { return q; },
      order:  function() { return ok([]); },
      limit:  function() { return ok([]); },
      maybeSingle: function() {
        if (t === 'app_settings')  return ok({ value: 'true' });
        if (t === 'user_settings') return ok({ keywords: [] });
        return ok(null);
      },
      single: function() { return this.maybeSingle(); },
      upsert: function() { return ok(null); },
      insert: function() { return ok(null); },
      update: function() { return q; }
    };
    return q;
  }
  var client = {
    auth: {
      getSession: function() {
        return Promise.resolve({ data: { session: null }, error: null });
      },
      signOut:       function() { return Promise.resolve({ error: null }); },
      signInWithOtp: function() { return Promise.resolve({ error: null }); }
    },
    from: function(t) { return bq(t); },
    channel: function() { return { on: function() { return this; }, subscribe: function() { return this; } }; }
  };
  window.supabase = { createClient: function() { return client; } };
  console.log('[STUB] unauth installed, session=null');
})();
"""

STUB_AUTH = """\
(function() {
  function ok(d) { return Promise.resolve({ data: d, error: null }); }
  function bq(t) {
    var q = {
      select: function() { return q; },
      eq:     function() { return q; },
      order:  function() { return ok([]); },
      limit:  function() { return ok([]); },
      maybeSingle: function() {
        if (t === 'app_settings')  return ok({ value: 'true' });
        if (t === 'user_settings') return ok({ keywords: [] });
        return ok(null);
      },
      single: function() { return this.maybeSingle(); },
      upsert: function() { return ok(null); },
      insert: function() { return ok(null); },
      update: function() { return q; }
    };
    return q;
  }
  var client = {
    auth: {
      getSession: function() {
        return Promise.resolve({
          data: { session: { user: { id: 'test-uid-1', email: 'smoke@example.com' } } },
          error: null
        });
      },
      signOut:       function() { return Promise.resolve({ error: null }); },
      signInWithOtp: function() { return Promise.resolve({ error: null }); }
    },
    from: function(t) { return bq(t); },
    channel: function() { return { on: function() { return this; }, subscribe: function() { return this; } }; }
  };
  window.supabase = { createClient: function() { return client; } };
  console.log('[STUB] auth installed, session=smoke@example.com');
})();
"""


def attach_stub(context, mode: str) -> None:
    script = STUB_AUTH if mode == "onboarding" else STUB_UNAUTH

    # 1) init_script — 페이지 JS보다 반드시 먼저 실행
    context.add_init_script(script=script)

    # 2) 네트워크 인터셉트 — supabase CDN 스크립트 자체를 스텁으로 교체
    #    app.html 은 https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2 를 사용
    def _fulfill(route):
        print(f"[INTERCEPT] {route.request.url}", file=sys.stderr)
        route.fulfill(status=200, content_type="application/javascript", body=script)

    context.route("**cdn.jsdelivr.net**supabase**", _fulfill)
    context.route("**unpkg.com**supabase**", _fulfill)
    context.route("**supabase**", _fulfill)          # 혹시 모를 다른 CDN 대비


def _diag(page):
    page.on("console",   lambda m: print(f"[CON:{m.type}] {m.text}", file=sys.stderr))
    page.on("pageerror", lambda e: print(f"[PAGEERROR] {e}",         file=sys.stderr))
    page.on("request",   lambda r: print(f"[REQ] {r.url}",          file=sys.stderr)
            if r.resource_type == "script" else None)


# ── 테스트 1: index.html CTA → auth overlay ────────────────────────────────

def check_index_overlay(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "index")
    page = ctx.new_page()
    _diag(page)
    page.goto(f"{BASE_URL}/index.html", wait_until="domcontentloaded")
    page.get_by_role("button", name=re.compile("무료로 시작하기")).first.click()
    display = page.evaluate(
        "() => getComputedStyle(document.getElementById('auth-overlay')).display"
    )
    if display != "block":
        raise AssertionError("index auth overlay did not open from CTA click")
    if not (page.is_visible("#view-login") or page.is_visible("#view-waitlist")):
        raise AssertionError("auth overlay opened but no auth/waitlist view visible")
    ctx.close()


# ── 테스트 2: app.html 비인증 → index.html 리다이렉트 ──────────────────────

def check_app_unauth_redirect(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "unauth_app")
    page = ctx.new_page()
    _diag(page)
    page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")

    # domcontentloaded 직후 스텁 설치 확인
    stub_ok = page.evaluate("""() => {
      try {
        var c = window.supabase && window.supabase.createClient('x','y');
        return c && c.auth ? 'ok' : 'no auth';
      } catch(e) { return 'err:' + e.message; }
    }""")
    print(f"[DIAG] stub_check={stub_ok}", file=sys.stderr)

    try:
        page.wait_for_url(f"{BASE_URL}/index.html", timeout=6000)
    except PlaywrightTimeoutError:
        print(f"[DIAG] title={page.title()}", file=sys.stderr)
        print(f"[DIAG] body[:200]={page.inner_text('body')[:200]}", file=sys.stderr)

    if "index.html" not in page.url:
        raise AssertionError(f"redirect 미발생. 현재 URL: {page.url}")
    ctx.close()


# ── 테스트 3: 온보딩 + 키워드 모달 ────────────────────────────────────────

def check_onboarding_keyword_modal(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "onboarding")
    page = ctx.new_page()
    _diag(page)
    page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")
    page.wait_for_selector(".onboarding-title", timeout=10000)
    if not page.evaluate("() => document.getElementById('kw-modal').classList.contains('open')"):
        raise AssertionError("온보딩 키워드 모달이 열리지 않음")
    page.fill("#kw-add-input", "테스트키워드")
    page.click(".kw-add-btn")
    page.wait_for_timeout(400)
    if "테스트키워드" not in page.locator("#kw-manage-list").inner_text():
        raise AssertionError("키워드 추가 플로우 실패")
    ctx.close()


def main() -> int:
    server = start_static_server()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                check_index_overlay(browser)
                check_app_unauth_redirect(browser)
                check_onboarding_keyword_modal(browser)
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        print(f"[SMOKE_GATE] Timeout: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[SMOKE_GATE] Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()

    print("[SMOKE_GATE] PASS - login/onboarding/keyword flow validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
