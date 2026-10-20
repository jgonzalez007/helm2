"""Automatic privacy mode: hide the panel when something is on the screen with you.

What this can honestly detect, and what it cannot:

* **A meeting app in the foreground** — reliable on Windows from the foreground
  window's executable. This is the signal that actually fires in practice.
* **Zoom actively sharing** — `cpthost.exe` is Zoom's share host, so its presence in
  front is a real share signal rather than a proxy for one.
* **A fullscreen window** — used to keep the island from popping over one.
* **Teams or Slack sharing specifically** — there is no public signal for this. HELM
  does not claim it. A Teams window in front is treated as "a meeting is happening",
  which is what the UI says, instead of "you are screen sharing", which it does not know.

`decide()` is pure and takes the already-read window facts, so the rules are testable
without a Windows desktop. Everything platform-specific is in `_foreground()`, which
returns None where it has no implementation — macOS keeps the panic hotkey and the
manual toggle, and Settings says so rather than pretending the watcher is running.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

from .bus import EventBus

# Executables that mean a meeting is on screen. Lowercase, matched exactly.
MEETING_APPS = {
    "ms-teams.exe", "teams.exe",              # Teams, new and classic
    "zoom.exe",                               # Zoom main window
    "webex.exe", "webexmta.exe", "atmgr.exe",  # Webex
    "gotomeeting.exe", "g2mui.exe",
    "bluejeans.exe",
}

# Executables that mean a share is actually running, not merely that a meeting is open.
SHARE_APPS = {
    "cpthost.exe",        # Zoom's screen-share host process
    "zoomshare.exe",
}


@dataclass
class PrivacyState:
    on: bool = False
    reason: str = ""
    app: str = ""
    manual: bool = False
    watching: bool = False     # False on a platform with no detector

    def to_dict(self) -> dict:
        return {"on": self.on, "reason": self.reason, "app": self.app,
                "manual": self.manual, "watching": self.watching}


@dataclass
class Window:
    exe: str = ""
    title: str = ""
    fullscreen: bool = False


def decide(win: Window | None, apps: set[str]) -> tuple[bool, str]:
    """Should the panel hide, and why. Pure: no window handles, no platform calls."""
    if win is None or not win.exe:
        return False, ""
    exe = win.exe.lower()
    if exe in SHARE_APPS:
        return True, "a screen share is running"
    if exe in {a.lower() for a in apps}:
        return True, "a meeting app is in the foreground"
    return False, ""


def _foreground() -> Window | None:
    """The foreground window's executable, title and whether it fills the screen."""
    if sys.platform != "win32":
        return None
    try:
        import win32api
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ""
        if pid:
            # QUERY_LIMITED_INFORMATION works without elevation; QUERY_INFORMATION
            # fails on processes running at a higher integrity level.
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                exe = win32process.GetModuleFileNameEx(handle, 0).rsplit("\\", 1)[-1]
            finally:
                win32api.CloseHandle(handle)

        rect = win32gui.GetWindowRect(hwnd)
        monitor = win32api.GetMonitorInfo(
            win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))
        fullscreen = tuple(rect) == tuple(monitor["Monitor"])
        return Window(exe=exe, title=title, fullscreen=fullscreen)
    except Exception:
        # A foreground window can vanish mid-read, and some windows refuse a query.
        # Neither is worth a traceback every second and a half.
        return None


class PrivacyWatcher:
    """Polls the foreground window and publishes `privacy_changed` when it matters.

    Local only: no connector call, no token cost, no network. A 1.5 s poll of one
    window handle is cheap enough to run for the life of the app.
    """

    INTERVAL = 1.5

    def __init__(self, bus: EventBus, enabled: Callable[[], bool],
                 apps: Callable[[], set[str]]) -> None:
        self._bus = bus
        self._enabled = enabled
        self._apps = apps
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._auto = PrivacyState()
        self._manual = False
        self._last_window = Window()

    # ── state ───────────────────────────────────────────────────────────────

    def state(self) -> PrivacyState:
        """Manual wins: the panic hotkey can hide the panel when nothing triggered it,
        and can never be overridden by the watcher deciding things look fine."""
        if self._manual:
            return PrivacyState(on=True, reason="you pressed the panic hotkey",
                                app="", manual=True, watching=self.available)
        st = PrivacyState(**{**self._auto.to_dict(), "manual": False})
        st.watching = self.available
        return st

    @property
    def available(self) -> bool:
        return self._thread is not None and sys.platform == "win32"

    def set_manual(self, on: bool | None = None) -> PrivacyState:
        self._manual = (not self._manual) if on is None else bool(on)
        st = self.state()
        self._bus.publish("privacy_changed", st.to_dict())
        return st

    @property
    def foreground_is_fullscreen(self) -> bool:
        """The island must not pop over a fullscreen app."""
        return self._last_window.fullscreen

    # ── loop ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        if sys.platform != "win32":
            print("[helm] automatic privacy detection is Windows-only; "
                  "the panic hotkey and the manual toggle still work")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="helm-privacy")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            win = _foreground()
            if win is not None:
                self._last_window = win
            if not self._enabled():
                if self._auto.on:
                    self._auto = PrivacyState()
                    self._bus.publish("privacy_changed", self.state().to_dict())
                continue
            on, reason = decide(win, self._apps())
            if on == self._auto.on and reason == self._auto.reason:
                continue
            self._auto = PrivacyState(on=on, reason=reason,
                                      app=(win.exe if win and on else ""))
            self._bus.publish("privacy_changed", self.state().to_dict())
