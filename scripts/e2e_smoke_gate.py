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
    signup_open = "true"
    if mode == "onboarding":
        keywords_payload = "[]"
    else:
        keywords_payload = "[]"

    return f"""
(() => {{
  function ok(data) {{
    return Promise.resolve({{ data, error: null }});
  }}

  function buildQuery(table) {{
    const state = {{ table, filters: {{}} }};
    const q = {{
      select() {{ return q; }},
      eq(key, val) {{ state.filters[key] = val; return q; }},
      order() {{ return resolveRows(); }},
      limit() {{ return resolveRows(); }},
      maybeSingle() {{ return resolveSingle(); }},
      single() {{ return resolveSingle(); }},
      upsert() {{ return ok(null); }},
      insert() {{ return ok(null); }},
      update() {{ return q; }},
    }};

    function resolveSingle() {{
      if (state.table === 'app_settings') {{
        return ok({{ value: '{signup_open}' }});
      }}
      if (state.table === 'user_settings') {{
        return ok({{ keywords: {keywords_payload} }});
      }}
      return ok(null);
    }}

    function resolveRows() {{
      if (state.table === 'industry_map')    {{ return ok([]); }}
      if (state.table === 'reports')         {{ return ok([]); }}
      if (state.table === 'brief_employees') {{ return ok([]); }}
      if (state.table === 'agents')          {{ return ok([]); }}
      return ok([]);
    }}

    return q;
  }}

  window.supabase = {{
    createClient: function() {{
      return {{
        auth: {{
          getSession: async () => ({{
            data: {{
              session: {authenticated}
                ? {{
                    user: {{
                      id: 'test-user-1',
                      email: 'smoke-user@example.com'
                    }}
                  }}
                : null
            }},
            error: null
          }}),
          signOut: async () => ({{ error: null }}),
          signInWithOtp: async () => ({{ error: null }}),
        }},
        from: function(table) {{
          return buildQuery(table);
        }},
        channel: function() {{
          return {{
            on: function() {{ return this; }},
            subscribe: function() {{ return this; }},
          }};
        }},
      }};
    }}
  }};
}})();
"""


def attach_stub(context, mode: str) -> None:
    script = supabase_stub_script(mode)

    def _fulfill(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=script,
        )

    context.route("**/@supabase/supabase-js@2*", _fulfill)


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

    # goto 전에 navigation 이벤트를 등록해야 리다이렉트를 놓치지 않음
    with page.expect_navigation(url="**/index.html", timeout=15000):
        page.goto(f"{BASE_URL}/app.html", wait_until="domcontentloaded")

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
