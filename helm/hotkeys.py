"""Global hotkeys. Windows uses RegisterHotKey; elsewhere this is a no-op.

Degrading to a no-op is deliberate: every hotkey also has an in-app control, so a
platform without a global-hotkey path loses convenience, not capability.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

# name -> (modifier string, virtual key). Mirrored in ui/js/keys.js for the "?" overlay.
BINDINGS = {
    "toggle_panel": ("ctrl+shift", "H"),
    "ask_bar": ("ctrl+shift", "K"),
    "focus_queue": ("ctrl+shift", "F"),
    "panic": ("ctrl+shift", "OEM_5"),      # Ctrl+Shift+\  — blurs everything at once
}

_MODS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_VK = {**{c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}, "OEM_5": 0xDC}


class Hotkeys:
    def __init__(self) -> None:
        self.available = False
        self._handlers: dict[str, Callable[[], None]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def on(self, name: str, fn: Callable[[], None]) -> None:
        if name not in BINDINGS:
            raise ValueError(f"unknown hotkey {name!r}")
        self._handlers[name] = fn

    def start(self) -> None:
        if sys.platform != "win32":
            print("[helm] global hotkeys unavailable on this platform; in-app controls still work")
            return
        try:
            import win32con  # noqa: F401
        except ImportError:
            print("[helm] pywin32 missing; global hotkeys disabled")
            return
        self._thread = threading.Thread(target=self._win_loop, daemon=True, name="helm-hotkeys")
        self._thread.start()
        self.available = True

    def stop(self) -> None:
        self._stop.set()

    def _win_loop(self) -> None:
        # RegisterHotKey binds to the calling thread, so the message pump has to
        # live in this same thread.
        import win32api
        import win32con
        import win32gui

        ids: dict[int, str] = {}
        for i, (name, (mods, key)) in enumerate(BINDINGS.items(), start=1):
            flags = 0
            for m in mods.split("+"):
                flags |= _MODS.get(m, 0)
            try:
                win32gui.RegisterHotKey(None, i, flags, _VK[key])
                ids[i] = name
            except Exception as e:
                print(f"[helm] hotkey {name} not registered ({e}); another app may own it")

        try:
            while not self._stop.is_set():
                rc, msg = win32gui.PeekMessage(None, 0, 0, win32con.PM_REMOVE)
                if rc and msg[1] == win32con.WM_HOTKEY:
                    name = ids.get(msg[2])
                    fn = self._handlers.get(name or "")
                    if fn:
                        try:
                            fn()
                        except Exception as e:
                            print(f"[helm] hotkey {name} handler failed: {e}")
                win32api.Sleep(40)
        finally:
            for i in ids:
                try:
                    win32gui.UnregisterHotKey(None, i)
                except Exception:
                    pass
