"""Time candidate calendar prompts against the real connector.

The first version asked for 15 fields per event, including a body excerpt, and blew
a 180 s timeout. This measures cheaper shapes so the sweep fits the cadence.

    python -m tests.tune_prompt
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from helm import claude_cli as cli

now = datetime.now().astimezone()
AFTER = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S")
BEFORE = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")

MINIMAL = f"""Call outlook_calendar_search with query "*", afterDateTime "{AFTER}", \
beforeDateTime "{BEFORE}", limit 25.

Reply with ONLY a JSON array, one object per event, keys exactly:
id, subject, start, end, timezone, organizer, organizer_email, attendee_emails,
response_status, join_url, has_agenda

start and end are the tool's wall-clock dateTime strings, copied verbatim, no time
zone conversion. timezone is the tool's timeZone string. response_status is one of
"organizer", "accepted", "tentative", "declined", "none". has_agenda is true only if
the event has body content. attendee_emails is an array of addresses.
Use "" or [] for anything the tool did not give you. Do not look anything up beyond
this one search. If there are no events, reply [].
"""

SEARCH_ONLY = f"""Call outlook_calendar_search with query "*", afterDateTime "{AFTER}", \
beforeDateTime "{BEFORE}", limit 25. Make exactly one tool call and do not read any \
event resource.

Reply with ONLY a JSON array from that one result, keys exactly:
id, subject, start, end, timezone, organizer, organizer_email, attendee_emails,
response_status, join_url, has_agenda

Copy start, end and timezone verbatim from the tool. Use "" or [] for anything the
result does not contain. If there are no events, reply [].
"""


def run(label: str, prompt: str, timeout: int) -> None:
    print(f"\n=== {label} (timeout {timeout} s) ===")
    t0 = time.perf_counter()
    try:
        reply, elapsed = cli.run_once(prompt, cli.CALENDAR_READ, timeout=timeout)
    except cli.ClaudeUnavailable as e:
        print(f"  FAILED after {time.perf_counter() - t0:.1f} s: {e}")
        return
    parsed = cli.extract_json(reply)
    print(f"  {elapsed / 1000:.1f} s, {len(reply)} chars, "
          f"parsed={type(parsed).__name__} n={len(parsed) if isinstance(parsed, list) else '-'}")
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        print(f"  keys: {sorted(first) if isinstance(first, dict) else first}")
        print(f"  first: {str(first)[:300]}")
    elif not isinstance(parsed, list):
        print(f"  raw: {reply[:300]}")


if __name__ == "__main__":
    run("search only, one call", SEARCH_ONLY, 300)
    run("minimal fields", MINIMAL, 300)
