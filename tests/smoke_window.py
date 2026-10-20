"""Launch the real windows, show the panel, read back the rendered DOM, then quit.

Catches what the headless checks cannot: a WebView2 failure, a broken ES module
import, a JS exception during the first render.

    python -m tests.smoke_window
"""

from __future__ import annotations

import json
import threading
import time

import webview

from helm import app as A
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.store import Store

RESULT: dict[str, object] = {}


def probe(api: A.Api) -> None:
    time.sleep(4)
    try:
        api._panel.show()
        # A hidden window only loads on show, and the API injection follows.
        for _ in range(40):
            time.sleep(0.5)
            if api._panel.evaluate_js("document.querySelectorAll('#nav .nav-item').length"):
                break
        RESULT["nav_items"] = api._panel.evaluate_js(
            "document.querySelectorAll('#nav .nav-item').length")
        RESULT["rows"] = api._panel.evaluate_js("document.querySelectorAll('.row').length")
        RESULT["greeting"] = api._panel.evaluate_js("document.getElementById('greeting').innerText")
        RESULT["statuses"] = api._panel.evaluate_js("document.getElementById('statuses').innerText")
        RESULT["badges"] = api._panel.evaluate_js("document.querySelectorAll('.badge').length")
        RESULT["shortcuts"] = api._panel.evaluate_js(
            "document.querySelectorAll('#shortcutbar .sc').length")
        RESULT["firstrun_open"] = api._panel.evaluate_js(
            "document.getElementById('firstrun').dataset.open")
        for _ in range(40):
            if api._island.evaluate_js("document.getElementById('counts').innerText"):
                break
            time.sleep(0.5)
        RESULT["island_state"] = api._island.evaluate_js(
            "document.getElementById('island').dataset.state")
        RESULT["island_counts"] = api._island.evaluate_js(
            "document.getElementById('counts').innerText")
        RESULT["island_next"] = api._island.evaluate_js(
            "document.getElementById('next').innerText")
        # A JS error during module load leaves window.helm without its handlers.
        RESULT["bridge_wired"] = api._panel.evaluate_js("typeof window.helm.onChatToken")
        RESULT["js_error"] = api._panel.evaluate_js("window.helm.diag.lastError")
        RESULT["island_js_error"] = api._island.evaluate_js("window.helm.diag.lastError")

        # The riskiest UI path: open an item, stream a reply, get an approval card.
        api._panel.evaluate_js(
            "document.getElementById('firstrun').dataset.open='0';"
            "document.querySelector('.row').click()")
        for _ in range(30):
            time.sleep(0.4)
            if api._panel.evaluate_js("document.getElementById('drawer').dataset.open") == '1':
                break
        RESULT["drawer_open"] = api._panel.evaluate_js(
            "document.getElementById('drawer').dataset.open")
        RESULT["attached_rows"] = api._panel.evaluate_js(
            "document.querySelectorAll('#attached div').length")
        RESULT["chips"] = api._panel.evaluate_js("document.querySelectorAll('.chip').length")

        api._panel.evaluate_js("document.querySelector('[data-chip=\\\"draft reply\\\"]').click()")
        for _ in range(40):
            time.sleep(0.4)
            if api._panel.evaluate_js("document.querySelectorAll('.approval').length"):
                break
        RESULT["approval_cards"] = api._panel.evaluate_js(
            "document.querySelectorAll('.approval').length")
        RESULT["approval_recipient"] = api._panel.evaluate_js(
            "document.querySelector('.approval dd')?.innerText")
        # The screenshots caught the Send button sitting below the fold. The one
        # control that approves a send must never need scrolling to find.
        RESULT["send_visible"] = api._panel.evaluate_js("""
            (() => {
              const btn = document.querySelector('.approval [data-approve]');
              const log = document.getElementById('chatlog');
              if (!btn || !log) return 'missing';
              const b = btn.getBoundingClientRect();
              const l = log.getBoundingClientRect();
              return (b.top >= l.top - 1 && b.bottom <= l.bottom + 1) ? 'yes'
                : `clipped b=${Math.round(b.bottom)} l=${Math.round(l.bottom)}`;
            })()
        """)
        RESULT["approval_body_visible"] = api._panel.evaluate_js(
            "!!document.querySelector('.approval .body')?.innerText.trim()")
        RESULT["chatlog_metrics"] = api._panel.evaluate_js("""
            (() => {
              const l = document.getElementById('chatlog');
              const pane = document.getElementById('chatpane');
              return {scrollH: l.scrollHeight, clientH: l.clientHeight,
                      scrollTop: l.scrollTop, paneH: pane.clientHeight,
                      overflow: getComputedStyle(l).overflowY,
                      minH: getComputedStyle(l).minHeight,
                      drawerH: document.getElementById('drawer').clientHeight};
            })()
        """)
        RESULT["streamed_reply"] = api._panel.evaluate_js(
            "document.querySelector('.bubble.assistant')?.innerText.slice(0,40)")
        RESULT["sends_before_click"] = len(
            [a for a in api._store.action_log() if a["verb"] == "send"])

        # The flag tooltip must say the body was not read.
        api._panel.evaluate_js(
            "document.querySelector('.badge-pair').dispatchEvent("
            "new MouseEvent('mouseover',{bubbles:true}))")
        time.sleep(0.4)
        RESULT["tooltip"] = api._panel.evaluate_js(
            "document.getElementById('tooltip').innerText.replace(/\\n/g,' | ')")
        # Phase 6: privacy blur, and the focus queue carrying a waiting item.
        api._panel.evaluate_js("window.helm.panic()")
        for _ in range(20):
            time.sleep(0.3)
            if api._panel.evaluate_js("document.body.dataset.privacy") == '1':
                break
        RESULT["privacy_on"] = api._panel.evaluate_js("document.body.dataset.privacy")
        RESULT["privacy_banner"] = api._panel.evaluate_js(
            "getComputedStyle(document.getElementById('privacy-banner')).display")
        RESULT["island_blurred"] = api._island.evaluate_js(
            "document.body.dataset.privacy")
        RESULT["main_blurred"] = api._panel.evaluate_js(
            "getComputedStyle(document.getElementById('main')).filter")
        api._panel.evaluate_js("window.helm.panic()")
        for _ in range(20):
            time.sleep(0.3)
            if api._panel.evaluate_js("document.body.dataset.privacy") == '0':
                break
        RESULT["privacy_off"] = api._panel.evaluate_js("document.body.dataset.privacy")

        api._panel.evaluate_js(
            "document.getElementById('drawer').dataset.open='0';window.helm.go('focus')")
        time.sleep(0.6)
        RESULT["focus_card"] = api._panel.evaluate_js(
            "document.querySelectorAll('.focus-card').length")
        # A waiting item is not necessarily last, so walk the whole queue.
        cards = []
        for _ in range(12):
            cards.append(api._panel.evaluate_js(
                "document.querySelector('.focus-card').innerText"
                ".split(String.fromCharCode(10)).join(' | ')"))
            api._panel.evaluate_js("document.dispatchEvent("
                                   "new KeyboardEvent('keydown',{key:'j',bubbles:true}))")
            time.sleep(0.25)
        RESULT["focus_queue_len"] = len({c.split(" | ")[0] for c in cards if c})
        RESULT["focus_followup"] = next(
            (c[:170] for c in cards if c and "Draft follow-up" in c), "")
        # Phase 7: search as you type, through the real index.
        api._panel.evaluate_js("window.helm.go('search')")
        time.sleep(0.5)
        typed = ""
        for ch in "forecast":
            typed += ch
            api._panel.evaluate_js(
                "(() => { const q = document.getElementById('q');"
                f" q.value = {typed!r};"
                " q.dispatchEvent(new Event('input', {bubbles: true})); })()")
            time.sleep(0.2)
        for _ in range(30):
            time.sleep(0.3)
            if api._panel.evaluate_js("document.querySelectorAll('.row--hit').length"):
                break
        RESULT["search_hits"] = api._panel.evaluate_js(
            "document.querySelectorAll('.row--hit').length")
        RESULT["search_marks"] = api._panel.evaluate_js(
            "document.querySelectorAll('.row--hit mark').length")
        RESULT["search_caret"] = api._panel.evaluate_js(
            "(() => { const q = document.getElementById('q');"
            " return q.value + '|' + q.selectionStart"
            " + '|' + (document.activeElement === q); })()")
        # A query that is an FTS5 syntax error must not break the box.
        hostile = json.dumps('NEAR( " AND ((')
        api._panel.evaluate_js(
            "(() => { const q = document.getElementById('q');"
            f" q.value = {hostile};"
            " q.dispatchEvent(new Event('input', {bubbles: true})); })()")
        time.sleep(1.2)
        RESULT["search_hostile"] = api._panel.evaluate_js(
            "document.getElementById('q') ? 'still there' : 'gone'")
        RESULT["search_js_error"] = api._panel.evaluate_js("window.helm.diag.lastError")
    except Exception as e:
        RESULT["error"] = f"{type(e).__name__}: {e}"
    finally:
        for w in (api._island, api._panel):
            try:
                w.destroy()
            except Exception:
                pass


def main() -> int:
    store = Store(A.data_dir() / "smoke.db") if hasattr(A, "data_dir") else Store()
    from helm.store import data_dir
    store = Store(data_dir() / "smoke.db")
    store.reset_demo()
    for key, value in DEMO_SETTINGS.items():
        store.set_setting(key, value)
    store.set_setting("first_run_done", False)
    if not store.shortcuts():
        for label, url, group in A.DEFAULT_SHORTCUTS:
            store.add_shortcut(label, url, group)

    api = A.Api(store, EventBus(), demo=True)
    api.sync()

    api._island = webview.create_window(
        "HELM", str(A.UI / "island.html"), js_api=api,
        width=A.ISLAND_W, height=A.ISLAND_H, x=A._island_x(), y=0,
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        transparent=True, background_color="#0C0F14",
    )
    api._panel = webview.create_window(
        "HELM", str(A.UI / "panel.html"), js_api=api,
        width=A.PANEL_W, height=A.PANEL_H, hidden=True, background_color="#0C0F14",
    )
    api.wire_bus()

    # private_mode gives a throwaway WebView2 profile. A shared one cached ui/*.css
    # and ui/*.js between runs, so this test was quietly verifying stale files.
    webview.start(lambda: threading.Thread(target=probe, args=(api,), daemon=True).start(),
                  private_mode=True, debug=False)

    print("\n--- rendered DOM ---")
    for k, v in RESULT.items():
        print(f"{k}: {v!r}")

    if "error" in RESULT:
        return 1
    ok = (RESULT.get("nav_items") == 9
          and (RESULT.get("rows") or 0) > 0
          and (RESULT.get("badges") or 0) > 0
          and RESULT.get("bridge_wired") == "function"
          and not RESULT.get("js_error")
          and RESULT.get("drawer_open") == "1"
          and (RESULT.get("chips") or 0) > 0
          and RESULT.get("approval_cards") == 1
          # The Send button must be on screen without scrolling.
          and RESULT.get("send_visible") == "yes"
          and RESULT.get("approval_body_visible") is True
          # An approval card must exist while nothing has been sent.
          and RESULT.get("sends_before_click") == 0
          and "not read" in (RESULT.get("tooltip") or "")
          # Privacy mode blurs both windows and shows its banner, then releases.
          and RESULT.get("privacy_on") == "1"
          and RESULT.get("privacy_banner") == "flex"
          and RESULT.get("island_blurred") == "1"
          and "blur" in (RESULT.get("main_blurred") or "")
          and RESULT.get("privacy_off") == "0"
          # The focus queue reaches a waiting item and offers to chase it.
          and RESULT.get("focus_card") == 1
          and bool(RESULT.get("focus_followup"))
          # The island has to paint its counts, not just load its module.
          and bool(RESULT.get("island_counts"))
          # Search runs against the real index and highlights the match.
          and (RESULT.get("search_hits") or 0) > 0
          and (RESULT.get("search_marks") or 0) > 0
          # Typing must not steal the caret.
          and (RESULT.get("search_caret") or "").startswith("forecast|8|true")
          and RESULT.get("search_hostile") == "still there"
          and not RESULT.get("search_js_error"))
    print("\nPASS" if ok else "\nFAIL — the UI loaded but did not render as expected")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
