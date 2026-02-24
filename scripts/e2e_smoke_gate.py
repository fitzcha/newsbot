#!/usr/bin/env python3
"""
Minimal end-to-end smoke gate for critical onboarding flow.

Checks:
1) index.html "start" CTA opens auth overlay.
2) app.html redirects unauthenticated user to index.html.
3) app.html authenticated new user sees onboarding + keyword modal,
   and keyword can be added from modal.
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
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return


def start_static_server() -> ThreadingHTTPServer:
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), QuietStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def supabase_stub_script(mode: str) -> str:
    authenticated = "true" if mode == "onboarding" else "false"

    # Build the stub as a plain string — no nested f-string issues.
    # Injected via add_init_script so it runs BEFORE any page JS,
    # making network interception timing irrelevant.
    return """
(function() {
  'use strict';

  function ok(data) {
    return Promise.resolve({ data: data, error: null });
  }

  function buildQuery(table) {
    var q = {
      select:      function() { return q; },
      eq:          function() { return q; },
      order:       function() { return resolveRows(); },
      limit:       function() { return resolveRows(); },
      maybeSingle: function() { return resolveSingle(); },
      single:      function() { return resolveSingle(); },
      upsert:      function() { return ok(null); },
      insert:      function() { return ok(null); },
      update:      function() { return q; }
    };

    function resolveSingle() {
      if (table === 'app_settings')  { return ok({ value: 'true' }); }
      if (table === 'user_settings') { return ok({ keywords: [] }); }
      return ok(null);
    }

    function resolveRows() {
      return ok([]);
    }

    return q;
  }

  var IS_AUTHENTICATED = """ + authenticated + """;

  var fakeClient = {
    auth: {
      getSession: async function() {
        return {
          data: {
            session: IS_AUTHENTICATED
              ? { user: { id: 'test-user-1', email: 'smoke-user@example.com' } }
              : null
          },
          error: null
        };
      },
      signOut:       async function() { return { error: null }; },
      signInWithOtp: async function() { return { error: null }; }
    },
    from: function(table) { return buildQuery(table); },
    channel: function() {
      return {
        on:        function() { return this; },
        subscribe: function() { return this; }
      };
    }
  };

  // Expose via both the global supabase shim AND window.supabase
  // so whatever import style app.html uses, it finds the stub.
  var fakeModule = { createClient: function() { return fakeClient; } };
  window.supabase = fakeModule;

  // Intercept dynamic import / ES-module style by defining a
  // non-writable property on window that shadows the CDN export.
  try {
    Object.defineProperty(window, '_supabaseStub', {
      value: fakeClient,
      writable: false
    });
  } catch (_) {}
})();
""";


def attach_stub(context, mode: str) -> None:
    script = supabase_stub_script(mode)

    # ── 1. init_script: runs before ANY page JS, no timing race ──────────
    context.add_init_script(script=script)

    # ── 2. network interception: covers CDN <script> tags too ─────────────
    def _fulfill(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=script,
        )

    # Match both versioned and unversioned CDN URLs for supabase-js
    context.route("**/*supabase*", _fulfill)


def check_index_overlay(browser) -> None:
    context = browser.new_context()
    attach_stub(context, "index")
    page = context.new_page()
    page.goto(f"{BASE_URL}/index.html", wait_until="domcontentloaded")

    page.get_by_role("button", name=re.compile("무료로 시작하기")).first.click()
    display = page.evaluate(
        "() => getComputedStyle(document.getElementById('auth-overlay')).display"
    )
    if display != "block":
        raise AssertionError("index auth overlay did not open from CTA click")

    login_visible = page.is_visible("#view-login")
    wait_visible = page.is_visible("#view-waitlist")
    if not (login_visible or wait_visible):
        raise AssertionError("index auth overlay opened but no auth/waitlist view is visible")

    context.close()


def check_app_unauth_redirect(browser) -> None:
    context = browser.new_context()
    attach_stub(context, "unauth_app")
    page = context.new_page()

    page.on("console", lambda msg: print(f"[BROWSER:{msg.type}] {msg.text}", file=sys.stderr))
    page.on("pageerror", lambda err: print(f"[BROWSER:pageerror] {err}", file=sys.stderr))

    page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")

    # Wait for the client-side redirect (JS location change), up to 5 s
    try:
        page.wait_for_url(f"{BASE_URL}/index.html", timeout=5000)
    except PlaywrightTimeoutError:
        pass  # fall through to assertion for a clear error message

    current_url = page.url
    print(f"[DEBUG] current url after redirect wait: {current_url}", file=sys.stderr)

    if "index.html" not in current_url:
        raise AssertionError(f"redirect did not happen. still on: {current_url}")

    context.close()


def check_onboarding_keyword_modal(browser) -> None:
    context = browser.new_context()
    attach_stub(context, "onboarding")
    page = context.new_page()
    page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")

    page.wait_for_selector(".onboarding-title", timeout=10000)
    modal_open = page.evaluate(
        "() => document.getElementById('kw-modal').classList.contains('open')"
    )
    if not modal_open:
        raise AssertionError("onboarding keyword modal is not opened for new user")

    page.fill("#kw-add-input", "테스트키워드")
    page.click(".kw-add-btn")
    page.wait_for_timeout(400)
    manage_text = page.locator("#kw-manage-list").inner_text()
    if "테스트키워드" not in manage_text:
        raise AssertionError("keyword add flow failed in onboarding modal")

    context.close()


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
    except Exception as exc:  # noqa: BLE001
        print(f"[SMOKE_GATE] Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()

    print("[SMOKE_GATE] PASS - login/onboarding/keyword flow validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
