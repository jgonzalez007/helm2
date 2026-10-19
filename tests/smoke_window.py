"""Launch the real windows, show the panel, read back the rendered DOM, then quit.

Catches what the headless checks cannot: a WebView2 failure, a broken ES module
import, a JS exception during the first render.

    python -m tests.smoke_window
"""

from __future__ import annotations

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

    webview.start(lambda: threading.Thread(target=probe, args=(api,), daemon=True).start(),
                  private_mode=False, storage_path=str(data_dir() / "webview"), debug=False)

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
          # An approval card must exist while nothing has been sent.
          and RESULT.get("sends_before_click") == 0
          and "not read" in (RESULT.get("tooltip") or ""))
    print("\nPASS" if ok else "\nFAIL — the UI loaded but did not render as expected")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
