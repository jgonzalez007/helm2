"""See what the Slack connector can actually return, and how fast.

Phase 2 and 3 both went faster once the prompt was measured before it was designed
around. Same here: this prints raw shapes and timings for the candidate calls.

    python -m tests.tune_slack
"""

from __future__ import annotations

import json
import time

from helm import claude_cli as cli

DMS = """Call slack_list_user_channels with types "im,mpim" and limit 20.

Reply with ONLY a JSON array, one object per conversation, keys:
id, name, type. If there are none, reply [].
"""

MENTIONS = """Search Slack for messages that mention me in the last 3 days.

Reply with ONLY a JSON array, one object per message, keys:
  id          the message timestamp or id
  channel     the channel name
  channel_id  the channel id
  author      the sender's display name
  author_id   the sender's id
  text        the message text, up to 200 characters
  sent_at     the message timestamp, copied verbatim
  permalink   a link to the message, or ""
If there are none, reply [].
"""

UNREAD_DMS = """Call slack_list_user_channels with types "im" and limit 10, then read \
the most recent 3 messages from each of the 5 most recently active of those DMs.

Reply with ONLY a JSON array, one object per message, keys:
  id, channel, channel_id, author, author_id, text, sent_at, is_me
Set is_me true if I sent it. Up to 200 characters of text. If there are none, reply [].
"""

ME = """Call slack_read_user_profile for me.

Reply with ONLY a JSON object with keys: id, name, real_name, email.
"""


def run(label: str, prompt: str, timeout: int = 420) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    t0 = time.perf_counter()
    try:
        reply, elapsed = cli.run_once(prompt, cli.SLACK_READ, timeout=timeout)
    except cli.ClaudeUnavailable as e:
        print(f"FAILED after {time.perf_counter() - t0:.1f} s: {e}")
        return
    print(f"{elapsed / 1000:.1f} s, {len(reply)} chars")
    parsed = cli.extract_json(reply)
    print(f"parsed: {type(parsed).__name__}")
    if isinstance(parsed, list):
        print(f"  {len(parsed)} entries")
        for entry in parsed[:3]:
            print(f"    {json.dumps(entry)[:320]}")
        if parsed and isinstance(parsed[0], dict):
            print(f"  keys: {sorted(parsed[0])}")
    elif isinstance(parsed, dict):
        print(f"  {json.dumps(parsed)[:320]}")
    else:
        print(f"  raw: {reply[:400]}")


if __name__ == "__main__":
    av = cli.probe_availability()
    print(f"Slack connector: {'connected' if av.slack else av.reason('Slack')}")
    if not av.slack:
        raise SystemExit(1)
    run("A: who am I", ME, 240)
    run("B: DM and group-DM list", DMS, 300)
    run("C: mentions of me, last 3 days", MENTIONS, 420)
    run("D: recent DM messages", UNREAD_DMS, 420)
