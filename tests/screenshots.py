"""Launch HELM in demo mode and photograph each view.

Not a test — a way to look at the real rendered UI without sitting and clicking.
Writes PNGs next to the repo and closes the windows again.

    python -m tests.screenshots [outdir]
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import webview
from PIL import ImageGrab

from helm import app as A
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.store import Store, data_dir

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots")

# (filename, what to run in the page first, how long to settle)
SHOTS = [
    ("01-today", "helm.go('today')", 1.2),
    ("02-focus", "helm.go('focus')", 1.2),
    ("03-inbox", "helm.go('inbox')", 1.2),
    ("04-calendar", "helm.go('calendar')", 1.2),
    ("05-recap", "helm.go('recap')", 1.2),
    ("06-shortcuts", "helm.go('shortcuts')", 1.2),
    ("07-settings", "helm.go('settings')", 1.5),
    ("08-actions", "helm.go('actions')", 1.2),
]

RESULT: dict[str, object] = {}


def window_rect(title: str):
    """Find a top-level window by title and return its screen rectangle."""
    import win32gui

    found: list[tuple] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetWindowText(hwnd) != title:
            return
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] - rect[0] > 40 and rect[3] - rect[1] > 4:
            found.append(rect)

    win32gui.EnumWindows(visit, None)
    return found


def dpi_scale() -> float:
    """Physical pixels per logical pixel.

    GetWindowRect reports logical units here while ImageGrab works in physical ones,
    so on a 125% display an unscaled bbox captures the wrong region — the first run
    photographed the window behind HELM.
    """
    import win32api
    import win32con

    logical = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    physical = ImageGrab.grab(all_screens=True).size[0]
    return physical / logical if logical else 1.0


SCALE = 1.0


def grab(rect, path: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    box = tuple(round(v * SCALE) for v in rect)
    ImageGrab.grab(bbox=box, all_screens=True).save(path)


def grab_panel(path: Path) -> None:
    """Find the panel again for every shot, and bring it forward first."""
    import win32con
    import win32gui

    rects = window_rect("HELM")
    if not rects:
        raise RuntimeError("the HELM panel window disappeared")
    rect = max(rects, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    for hwnd in _handles("HELM"):
        if win32gui.GetWindowRect(hwnd) == rect:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            break
    time.sleep(0.25)
    grab(rect, path)


def _handles(title: str) -> list[int]:
    import win32gui

    out: list[int] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            out.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return out


def shoot(api: A.Api) -> None:
    global SCALE
    saved: list[str] = []
    try:
        time.sleep(4)
        SCALE = dpi_scale()
        RESULT["scale"] = SCALE

        # The island announces itself at launch; catch it before it folds away.
        api._island.evaluate_js("helm.hold && helm.hold()")
        islands = window_rect("HELM")
        if islands:
            top = min(islands, key=lambda r: r[1])
            grab(top, OUT / "00-island.png")
            saved.append("00-island.png")

        api._panel.show()
        # ImageGrab photographs the screen, not the window, so anything overlapping
        # shows through. One run captured the editor sitting on top of HELM.
        try:
            api._panel.on_top = True
        except Exception:
            pass
        for _ in range(40):
            time.sleep(0.5)
            if api._panel.evaluate_js("document.querySelectorAll('#nav .nav-item').length"):
                break
        api._panel.evaluate_js("document.getElementById('firstrun').dataset.open='1'")
        time.sleep(1.0)

        rects = window_rect("HELM")
        panel = max(rects, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if rects else None
        if panel is None:
            RESULT["error"] = "could not find the panel window"
            return

        grab_panel(OUT / "09-first-run.png")
        saved.append("09-first-run.png")
        api._panel.evaluate_js("document.getElementById('firstrun').dataset.open='0'")

        for name, js, settle in SHOTS:
            api._panel.evaluate_js(js)
            time.sleep(settle)
            grab_panel(OUT / f"{name}.png")
            saved.append(f"{name}.png")

        # The item drawer, with a flag tooltip showing.
        api._panel.evaluate_js("helm.go('today')")
        time.sleep(1.0)
        api._panel.evaluate_js("document.querySelector('.row').click()")
        for _ in range(20):
            time.sleep(0.4)
            if api._panel.evaluate_js(
                    "document.getElementById('drawer').dataset.open") == "1":
                break
        time.sleep(1.0)
        grab_panel(OUT / "10-item-drawer.png")
        saved.append("10-item-drawer.png")

        # A draft, streamed, ending in an approval card.
        api._panel.evaluate_js(
            "document.querySelector('[data-chip=\\\"draft reply\\\"]').click()")
        for _ in range(40):
            time.sleep(0.4)
            if api._panel.evaluate_js("document.querySelectorAll('.approval').length"):
                break
        time.sleep(0.8)
        grab_panel(OUT / "11-approval-card.png")
        saved.append("11-approval-card.png")

        # The flag tooltip, which carries the "why".
        api._panel.evaluate_js(
            "document.querySelector('.badge-pair').dispatchEvent("
            "new MouseEvent('mouseover',{bubbles:true}))")
        time.sleep(0.8)
        grab_panel(OUT / "12-flag-tooltip.png")
        saved.append("12-flag-tooltip.png")

        # The keyboard overlay.
        api._panel.evaluate_js("document.getElementById('drawer').dataset.open='0';"
                               "document.getElementById('help').dataset.open='1'")
        time.sleep(0.8)
        grab_panel(OUT / "13-keyboard.png")
        saved.append("13-keyboard.png")

        # Privacy mode.
        api._panel.evaluate_js("document.getElementById('help').dataset.open='0';"
                               "helm.panic()")
        time.sleep(0.8)
        grab_panel(OUT / "14-privacy-mode.png")
        saved.append("14-privacy-mode.png")
        api._panel.evaluate_js("helm.panic()")

        # And the dark theme, since both are designed.
        api._panel.evaluate_js("document.documentElement.dataset.theme='dark';"
                               "helm.go('today')")
        time.sleep(1.2)
        grab_panel(OUT / "15-dark-today.png")
        saved.append("15-dark-today.png")

        RESULT["saved"] = saved
    except Exception as e:
        RESULT["error"] = f"{type(e).__name__}: {e}"
        RESULT["saved"] = saved
    finally:
        for w in (api._island, api._panel):
            try:
                w.destroy()
            except Exception:
                pass


def main() -> int:
    store = Store(data_dir() / "shots.db")
    store.reset_demo()
    for key, value in DEMO_SETTINGS.items():
        store.set_setting(key, value)
    store.set_setting("theme", "light")
    if not store.shortcuts():
        for label, url, group in A.DEFAULT_SHORTCUTS:
            store.add_shortcut(label, url, group)

    api = A.Api(store, EventBus(), demo=True)
    api.sync()

    api._island = webview.create_window(
        "HELM", str(A.UI / "island.html"), js_api=api,
        width=A.ISLAND_W, height=A.ISLAND_H, x=A._island_x(), y=0,
        frameless=True, easy_drag=False, on_top=True, resizable=False,
        transparent=True, background_color="#0C0F14")
    api._panel = webview.create_window(
        "HELM", str(A.UI / "panel.html"), js_api=api,
        width=A.PANEL_W, height=A.PANEL_H, hidden=True, background_color="#0C0F14")
    api.wire_bus()

    webview.start(lambda: threading.Thread(target=shoot, args=(api,), daemon=True).start(),
                  # Throwaway profile: a shared one serves cached CSS and JS, so a
                  # screenshot would show the previous build.
                  private_mode=True, debug=False)

    saved = RESULT.get("saved") or []
    for name in saved:
        print(f"  {OUT / name}")
    if RESULT.get("error"):
        print(f"\nstopped early: {RESULT['error']}")
    print(f"\n{len(saved)} screenshots in {OUT.resolve()}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
