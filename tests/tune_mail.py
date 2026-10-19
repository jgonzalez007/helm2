"""See what the mail prompt actually returns.

The first live sweep reported status "live" with zero rows, which means the reply
parsed as a dict but carried nothing usable. This prints the raw text.

    python -m tests.tune_mail
"""

from __future__ import annotations

import json
import time

from helm import claude_cli as cli
from helm.connectors.mail_source import MAIL_PROMPT

SIMPLE = """Call outlook_email_search with order "newest" and limit 5.

Reply with ONLY a JSON array, one object per email, keys: id, subject, from_email,
received. Copy the received timestamp verbatim. If there are none, reply [].
"""

SENT_ONLY = """Call outlook_email_search with folderName "Sent Items", order "newest", \
limit 5.

Reply with ONLY a JSON array, one object per email, keys: id, subject, to_emails,
sent. Copy the sent timestamp verbatim. If there are none, reply [].
"""


def run(label: str, prompt: str, timeout: int = 300) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    t0 = time.perf_counter()
    try:
        reply, elapsed = cli.run_once(prompt, cli.MAIL_READ, timeout=timeout)
    except cli.ClaudeUnavailable as e:
        print(f"FAILED after {time.perf_counter() - t0:.1f} s: {e}")
        return
    print(f"{elapsed / 1000:.1f} s, {len(reply)} chars")
    print(f"--- raw (first 1200) ---\n{reply[:1200]}")
    parsed = cli.extract_json(reply)
    print(f"--- parsed: {type(parsed).__name__} ---")
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            print(f"  {k}: {type(v).__name__} len={len(v) if hasattr(v, '__len__') else '-'}")
            if isinstance(v, list) and v:
                print(f"    first: {json.dumps(v[0])[:300]}")
    elif isinstance(parsed, list):
        print(f"  {len(parsed)} entries")
        if parsed:
            print(f"    first: {json.dumps(parsed[0])[:300]}")


if __name__ == "__main__":
    run("A: inbox only, 4 fields", SIMPLE)
    run("B: sent items only, 4 fields", SENT_ONLY)
    run("C: the real sweep prompt", MAIL_PROMPT.format(inbox_limit=10, sent_limit=5))
