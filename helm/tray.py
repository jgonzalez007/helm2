"""System tray icon. Degrades to a no-op if pystray has no backend here.

The icon is drawn at runtime rather than shipped as a file: HELM's mark is its own
( o ) helm wheel, not borrowed artwork.
"""

from __future__ import annotations

import threading
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAVE = True
except Exception:  # pragma: no cover - depends on the platform's tray support
    _HAVE = False


def _icon_image(size: int = 64):
    """A ship's-wheel glyph: ring, hub, four spokes. Original, drawn in code."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m, amber = size * 0.10, (232, 150, 44, 255)
    d.ellipse([m, m, size - m, size - m], outline=amber, width=max(2, size // 14))
    c, r = size / 2, size * 0.13
    d.ellipse([c - r, c - r, c + r, c + r], fill=amber)
    spoke = max(2, size // 16)
    d.line([c, m * 0.6, c, size - m * 0.6], fill=amber, width=spoke)
    d.line([m * 0.6, c, size - m * 0.6, c], fill=amber, width=spoke)
    return img


class Tray:
    def __init__(self) -> None:
        self.available = _HAVE
        self._icon = None
        self._thread: threading.Thread | None = None

    def start(self, on_open: Callable[[], None], on_island: Callable[[], None],
              on_settings: Callable[[], None], on_quit: Callable[[], None]) -> None:
        if not _HAVE:
            print("[helm] tray unavailable; use the window and hotkeys instead")
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open HELM", lambda *_: on_open(), default=True),
            pystray.MenuItem("Show island", lambda *_: on_island()),
            pystray.MenuItem("Settings", lambda *_: on_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: on_quit()),
        )
        self._icon = pystray.Icon("helm", _icon_image(), "HELM", menu)
        self._thread = threading.Thread(target=self._icon.run, daemon=True, name="helm-tray")
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
