"""One real calendar sweep through the approved connector. Read-only.

Not part of the normal suite: it needs the connectors, takes 60-120 s and reads
your actual calendar. Run it when you want to confirm the live path, or after any
change to the prompt or the parser.

    python -m tests.live_calendar            one sweep
    python -m tests.live_calendar --prep     one sweep, then a prep brief
"""

from __future__ import annotations

import sys
import time

from helm import claude_cli as cli
from helm.connectors.claude_source import CalendarSource
from helm.flags import Settings, classify

ME = "jose.gonzalez@sailpoint.com"


def main() -> int:
    print("checking connectors…")
    av = cli.probe_availability()
    print(f"  claude CLI:    {av.cli or 'NOT FOUND'}")
    print(f"  Microsoft 365: {'connected' if av.microsoft_365 else av.reason('Microsoft 365')}")
    if not av.microsoft_365:
        print("\nThe calendar source cannot run without the Microsoft 365 connector.")
        return 1

    source = CalendarSource(ME, ["sailpoint.com"], av)

    print("\nsweeping the calendar (expect 60-120 s)…")
    t0 = time.perf_counter()
    items = source.list_new("")
    elapsed = time.perf_counter() - t0
    status = source.status()

    print(f"\nstatus:   {status.health}")
    print(f"round-trip: {elapsed:.1f} s  (source reported {status.measured_latency_ms} ms)")
    if status.last_error:
        print(f"note:     {status.last_error}")
    print(f"events:   {len(items)}")

    if not items:
        print("\nNo events returned. That is a pass if your calendar really is clear "
              "for the next two days, and a failure otherwise — check the status above.")
        return 0 if status.health == "live" else 1

    cfg = Settings(internal_domains={"sailpoint.com"})
    print()
    for item in items:
        flag = classify(item, cfg)
        gaps = []
        if not item.has_agenda:
            gaps.append("no agenda")
        if item.rsvp_pending:
            gaps.append("RSVP pending")
        if item.conflicts_with:
            gaps.append(f"overlaps {len(item.conflicts_with)}")
        if item.external_attendees:
            gaps.append("external")
        print(f"  {item.starts_at[:16]}  {item.subject[:46]:48}"
              f"{flag.importance}/{flag.urgency:6} {', '.join(gaps) or '-'}")
        print(f"      organizer={item.sender or '?'}  attendees={len(item.attendees)}"
              f"  join={'yes' if item.join_url else 'no'}  id={item.id[:24]}")

    problems = []
    if any(not it.starts_at for it in items):
        problems.append("an event came back with no start time")
    if any(it.join_url and not it.join_url.startswith("https://") for it in items):
        problems.append("a join URL is not https")
    if len({it.id for it in items}) != len(items):
        problems.append("two events share an id, which would duplicate on resync")

    if "--prep" in sys.argv:
        target = items[0]
        print(f"\nprep brief for: {target.subject}\n" + "-" * 60)
        t1 = time.perf_counter()
        try:
            brief = source.prep_brief(target, on_token=lambda t: print(t, end="", flush=True))
            print(f"\n{'-' * 60}\ncold start: {time.perf_counter() - t1:.1f} s")
            if not brief.strip():
                problems.append("the prep brief came back empty")
            t2 = time.perf_counter()
            source.session().ask("In one short line, what is the single most useful thing "
                                 "I should say first in that meeting?")
            print(f"warm turn:  {time.perf_counter() - t2:.1f} s")
        except cli.ClaudeUnavailable as e:
            problems.append(f"prep brief failed: {e}")
        finally:
            source.close()

    print()
    for p in problems:
        print(f"PROBLEM: {p}")
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
