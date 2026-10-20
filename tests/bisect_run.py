"""Find which part of app.run() stops the windows appearing.

Replays run()'s stages with markers, and can disable the tray, hotkeys and privacy
watcher one at a time.

    python -m tests.bisect_run              # everything, like run.py --demo
    python -m tests.bisect_run no-tray      # ...without the tray
    python -m tests.bisect_run bare         # windows only
"""

from __future__ import annotations

import sys
import threading
import time

import webview

from helm import app as A
from helm.bus import EventBus
from helm.store import Store

MODE = sys.argv[1] if len(sys.argv) > 1 else "all"


def say(msg: str) -> None:
    print(f"[bisect] {msg}", flush=True)


def main() -> int:
    say(f"mode={MODE}")
    store = Store()
    say("store ok")
    api = A.Api(store, EventBus(), demo=True)
    say("api ok")
    api.sync()
    say("sync ok")
    x = A._island_x()
    say(f"island_x={x}  (webview.screens read before start)")

    api._island = webview.create_window(
        "HELM", str(A.UI / "island.html"), js_api=api,
        width=A.ISLAND_W, height=A.ISLAND_H, x=x, y=0,
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        transparent=True, background_color="#0C0F14",
    )
    say("island registered")
    api._panel = webview.create_window(
        "HELM", str(A.UI / "panel.html"), js_api=api,
        width=A.PANEL_W, height=A.PANEL_H, min_size=(760, 520),
        hidden=True, background_color="#0C0F14",
    )
    say("panel registered")
    api.wire_bus()
    say("bus wired")

    tray, keys = A.Tray(), A.Hotkeys()

    def start_background() -> None:
        say("start_background entered")
        if MODE in ("all", "no-keys", "no-privacy"):
            tray.start(on_open=api.open_panel, on_island=lambda: api._island.show(),
                       on_settings=api.open_panel, on_quit=lambda: None)
            say("tray started")
        if MODE in ("all", "no-tray", "no-privacy"):
            keys.start()
            say("hotkeys started")
        if MODE in ("all", "no-tray", "no-keys"):
            api._privacy.start()
            say("privacy started")
        say("start_background done")
        time.sleep(10)
        say("quitting")
        for w in (api._island, api._panel):
            try:
                w.destroy()
            except Exception as e:
                say(f"destroy failed: {e}")

    say("calling webview.start")
    webview.start(start_background, private_mode=False,
                  storage_path=str(store.path.parent / "webview"), debug=False)
    say("webview.start returned")
    keys.stop()
    tray.stop()
    api._privacy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
