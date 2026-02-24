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


# ── JS 스텁: 문자열 포매팅 없이 완전한 JS 파일 2개로 분리 ─────────────────────

STUB_UNAUTH = """\
(function() {
  var _fakeClient = {
    auth: {
      getSession: function() {
        return Promise.resolve({ data: { session: null }, error: null });
      },
      signOut: function() { return Promise.resolve({ error: null }); },
      signInWithOtp: function() { return Promise.resolve({ error: null }); }
    },
    from: function(t) { return _buildQuery(t); },
    channel: function() { return { on: function() { return this; }, subscribe: function() { return this; } }; }
  };

  function _ok(d) { return Promise.resolve({ data: d, error: null }); }

  function _buildQuery(table) {
    var q = {
      select: function() { return q; },
      eq: function() { return q; },
      order: function() { return _ok([]); },
      limit: function() { return _ok([]); },
      maybeSingle: function() { return _single(table); },
      single: function() { return _single(table); },
      upsert: function() { return _ok(null); },
      insert: function() { return _ok(null); },
      update: function() { return q; }
    };
    return q;
  }

  function _single(table) {
    if (table === 'app_settings') { return _ok({ value: 'true' }); }
    if (table === 'user_settings') { return _ok({ keywords: [] }); }
    return _ok(null);
  }

  window.supabase = { createClient: function() { return _fakeClient; } };
  console.log('[STUB] supabase unauth stub installed');
})();
"""

STUB_AUTH = """\
(function() {
  var _fakeClient = {
    auth: {
      getSession: function() {
        return Promise.resolve({
          data: { session: { user: { id: 'test-user-1', email: 'smoke@example.com' } } },
          error: null
        });
      },
      signOut: function() { return Promise.resolve({ error: null }); },
      signInWithOtp: function() { return Promise.resolve({ error: null }); }
    },
    from: function(t) { return _buildQuery(t); },
    channel: function() { return { on: function() { return this; }, subscribe: function() { return this; } }; }
  };

  function _ok(d) { return Promise.resolve({ data: d, error: null }); }

  function _buildQuery(table) {
    var q = {
      select: function() { return q; },
      eq: function() { return q; },
      order: function() { return _ok([]); },
      limit: function() { return _ok([]); },
      maybeSingle: function() { return _single(table); },
      single: function() { return _single(table); },
      upsert: function() { return _ok(null); },
      insert: function() { return _ok(null); },
      update: function() { return q; }
    };
    return q;
  }

  function _single(table) {
    if (table === 'app_settings') { return _ok({ value: 'true' }); }
    if (table === 'user_settings') { return _ok({ keywords: [] }); }
    return _ok(null);
  }

  window.supabase = { createClient: function() { return _fakeClient; } };
  console.log('[STUB] supabase AUTH stub installed');
})();
"""


def attach_stub(context, mode: str) -> None:
    script = STUB_AUTH if mode == "onboarding" else STUB_UNAUTH

    # 1) init_script: 페이지 JS보다 먼저 실행
    context.add_init_script(script=script)

    # 2) 네트워크 인터셉트: supabase CDN 요청을 스텁으로 대체
    def _fulfill(route):
        print(f"[STUB] intercepted: {route.request.url}", file=sys.stderr)
        route.fulfill(status=200, content_type="application/javascript", body=script)

    context.route("**/*supabase*", _fulfill)


# ── 진단용: app.html이 로드하는 모든 스크립트 URL 출력 ──────────────────────────

def _attach_diagnostics(page):
    page.on("console", lambda m: print(f"[CON:{m.type}] {m.text}", file=sys.stderr))
    page.on("pageerror", lambda e: print(f"[PAGEERROR] {e}", file=sys.stderr))
    page.on("request", lambda r: print(f"[REQ] {r.resource_type} {r.url}", file=sys.stderr)
            if r.resource_type in ("script", "fetch", "xhr") else None)
    page.on("response", lambda r: print(f"[RES] {r.status} {r.url}", file=sys.stderr)
            if r.request.resource_type in ("script", "fetch", "xhr") else None)


# ── 테스트 함수 ────────────────────────────────────────────────────────────────

def check_index_overlay(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "index")
    page = ctx.new_page()
    _attach_diagnostics(page)
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


def check_app_unauth_redirect(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "unauth_app")
    page = ctx.new_page()
    _attach_diagnostics(page)

    page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")

    # domcontentloaded 직후 supabase 스텁 상태 확인
    stub_check = page.evaluate("""() => {
      if (!window.supabase) return 'NO window.supabase';
      if (typeof window.supabase.createClient !== 'function') return 'createClient not a function';
      try {
        var c = window.supabase.createClient('x','y');
        return c && c.auth ? 'stub OK' : 'stub client missing auth';
      } catch(e) { return 'createClient threw: ' + e.message; }
    }""")
    print(f"[DIAG] stub_check: {stub_check}", file=sys.stderr)

    try:
        page.wait_for_url(f"{BASE_URL}/index.html", timeout=6000)
    except PlaywrightTimeoutError:
        # 추가 진단: 페이지 title, body 텍스트 일부
        print(f"[DIAG] page title: {page.title()}", file=sys.stderr)
        print(f"[DIAG] body snippet: {page.inner_text('body')[:300]}", file=sys.stderr)

    if "index.html" not in page.url:
        raise AssertionError(f"redirect 미발생. 현재 URL: {page.url}")
    ctx.close()


def check_onboarding_keyword_modal(browser) -> None:
    ctx = browser.new_context()
    attach_stub(ctx, "onboarding")
    page = ctx.new_page()
    _attach_diagnostics(page)
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
