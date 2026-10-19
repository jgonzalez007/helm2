"""In-process event bus. Sources publish, the UI subscribes, nothing polls the DOM."""

from __future__ import annotations

import threading
import traceback
from collections import defaultdict
from typing import Any, Callable

# The event names sources are allowed to emit. Keeping them in one list means a
# typo'd publish fails here rather than disappearing into a subscriber that
# never fires.
EVENTS = (
    "new_email",
    "slack_mention",
    "meeting_in_5",
    "source_status",
    "flag_changed",
    "item_changed",
    "action_logged",
    "privacy_changed",
)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[dict], None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event: str, fn: Callable[[dict], None]) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; add it to bus.EVENTS")
        with self._lock:
            self._subs[event].append(fn)

    def publish(self, event: str, payload: dict[str, Any] | None = None) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; add it to bus.EVENTS")
        with self._lock:
            listeners = list(self._subs[event])
        for fn in listeners:
            # One broken subscriber must not stop the others, and must not take
            # a polling thread down with it.
            try:
                fn(payload or {})
            except Exception:
                traceback.print_exc()
