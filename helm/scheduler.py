"""Polling schedule: faster during work hours, slower at night, paused in quiet hours.

Every sweep is a model turn with a real token cost, so the cadence is the main cost
control in HELM. The numbers come from the Step 0 measurements: a connector sweep
takes 66-97 s, which rules out anything tighter than a minute even if it were free.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime
from typing import Callable

from .bus import EventBus

# seconds between sweeps, per source
# Measured sweeps: calendar ~124-144 s, mail ~230 s (two tool calls). The Step 0
# plan said 2 minutes for mail, which a 230-second sweep cannot do — it would run
# back to back forever. Both are 10 minutes in work hours. The meeting countdown is
# a local timer, so freshness there costs nothing either way.
WORK_HOURS = {"mail": 600, "slack": 600, "calendar": 600}
OFF_HOURS = {"mail": 900, "slack": 900, "calendar": 1800}

WORK_START, WORK_END = dtime(8, 0), dtime(18, 0)
BACKOFF_CAP = 1800
# A wall-clock jump this much larger than the sleep we asked for means the machine
# suspended, so everything is resynced rather than waited out.
WAKE_DRIFT = 90


def _hhmm(text: str, fallback: dtime) -> dtime:
    try:
        h, m = text.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    s, e = _hhmm(start, dtime(19, 0)), _hhmm(end, dtime(7, 0))
    t = now.time()
    return (s <= t or t < e) if s > e else (s <= t < e)


def is_work_time(now: datetime) -> bool:
    return now.weekday() < 5 and WORK_START <= now.time() < WORK_END


def interval_for(source: str, now: datetime) -> int:
    table = WORK_HOURS if is_work_time(now) else OFF_HOURS
    return table.get(source, 600)


class Scheduler:
    """Drives `poll(source)` on a cadence, with backoff and a wake-up resync."""

    def __init__(self, bus: EventBus, poll: Callable[[str], bool],
                 sources: tuple[str, ...], quiet: Callable[[], tuple[str, str]]) -> None:
        self._bus = bus
        self._poll = poll
        self._sources = sources
        self._quiet = quiet
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._due: dict[str, float] = {}
        self._backoff: dict[str, int] = {s: 0 for s in sources}
        self.last_skip_reason = ""

    def start(self) -> None:
        if self._thread is not None:
            return
        now = time.monotonic()
        # Stagger the first sweeps: three 90-second connector calls at once would
        # make the first minute of the app feel wedged.
        for i, source in enumerate(self._sources):
            self._due[source] = now + i * 20
        self._thread = threading.Thread(target=self._loop, daemon=True, name="helm-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self, reason: str = "manual") -> None:
        """Force every source to sweep now — used on wake, reconnect, or a click."""
        now = time.monotonic()
        for source in self._sources:
            self._due[source] = now
            self._backoff[source] = 0
        self.last_skip_reason = ""
        self._bus.publish("source_status", {"resync": reason})
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now_wall = datetime.now()
            quiet_start, quiet_end = self._quiet()

            if in_quiet_hours(now_wall, quiet_start, quiet_end):
                self.last_skip_reason = f"quiet hours ({quiet_start}-{quiet_end})"
                self._sleep(60)
                continue

            if self.last_skip_reason.startswith("quiet hours"):
                # First tick after quiet hours: one catch-up sweep, not a slow drift
                # back into cadence.
                self.poke("after quiet hours")

            mono = time.monotonic()
            for source in self._sources:
                if self._stop.is_set() or mono < self._due.get(source, 0):
                    continue
                ok = False
                try:
                    ok = bool(self._poll(source))
                except Exception as e:  # a wedged source must not kill the loop
                    print(f"[helm] poll({source}) raised: {e}")
                base = interval_for(source, datetime.now())
                if ok:
                    self._backoff[source] = 0
                    self._due[source] = time.monotonic() + base
                else:
                    self._backoff[source] = min(
                        BACKOFF_CAP, (self._backoff[source] * 2) or base)
                    self._due[source] = time.monotonic() + self._backoff[source]

            self._sleep(5)

    def _sleep(self, seconds: float) -> None:
        """Sleep, then notice if the machine suspended while we were out."""
        before = time.time()
        self._wake.wait(seconds)
        self._wake.clear()
        if time.time() - before > seconds + WAKE_DRIFT:
            self.poke("woke from sleep")

    def describe(self) -> dict:
        """What the UI shows in Settings: the real cadence, not a promise."""
        now = datetime.now()
        mono = time.monotonic()
        return {
            "work_time": is_work_time(now),
            "paused": self.last_skip_reason,
            "sources": {s: {"interval_s": interval_for(s, now),
                            "next_in_s": max(0, int(self._due.get(s, mono) - mono)),
                            "backoff_s": self._backoff.get(s, 0)}
                        for s in self._sources},
        }
