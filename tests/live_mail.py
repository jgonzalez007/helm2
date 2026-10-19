"""One real mail sweep through the approved connector. Read and draft only.

Nothing is sent and nothing can be: MailSource.send refuses until phase 10.
This reads your actual inbox and sent items, takes a few minutes, and needs the
connectors.

    python -m tests.live_mail               sweep only
    python -m tests.live_mail --thread      sweep, then fetch one real thread
    python -m tests.live_mail --draft       sweep, fetch a thread, draft a reply
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from helm import claude_cli as cli
from helm.connectors.mail_source import MailSource, describe_voice
from helm.flags import Settings, classify
from helm.store import Store

ME = "jose.gonzalez@sailpoint.com"


def main() -> int:
    print("checking connectors…")
    av = cli.probe_availability()
    print(f"  claude CLI:    {av.cli or 'NOT FOUND'}")
    print(f"  Microsoft 365: {'connected' if av.microsoft_365 else av.reason('Microsoft 365')}")
    if not av.microsoft_365:
        return 1

    problems: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "live.db")
        source = MailSource(ME, ["sailpoint.com"], av, store=store)

        print("\nsweeping the inbox and sent items (expect 2-4 minutes)…")
        t0 = time.perf_counter()
        items = source.list_new("")
        elapsed = time.perf_counter() - t0
        status = source.status()

        print(f"\nstatus:     {status.health}")
        print(f"round-trip: {elapsed:.1f} s")
        if status.last_error:
            print(f"note:       {status.last_error}")
        print(f"rows:       {len(items)}")

        if status.health != "live":
            print("\nThe sweep did not succeed. Nothing below is real.")
            return 1

        # "live with zero rows" is the failure that looks like success. An inbox
        # really can be empty, but it is not the result this check is confirming,
        # so say so loudly rather than printing PASS over nothing.
        if not items:
            print("\nThe sweep succeeded but returned no messages. Either the mailbox "
                  "really is empty for this window, or the prompt is not getting data. "
                  "Re-run tests.tune_mail to see the raw reply.")
            return 1

        cfg = Settings(internal_domains={"sailpoint.com"})
        inbox = [i for i in items if i.state != "waiting"]
        waiting = [i for i in items if i.state == "waiting"]

        print(f"\n── inbox ({len(inbox)}) " + "─" * 40)
        for item in inbox:
            store.upsert_item(item)
            flag = classify(item, cfg)
            reason = source.reply_reason(item.id)
            mark = "REPLY" if item.state == "needs_me" else "  -  "
            print(f"  {mark} {item.received_at[:16]}  {item.sender[:20]:22}"
                  f"{item.subject[:40]:42}{flag.importance}/{flag.urgency}")
            print(f"        {reason}")

        print(f"\n── waiting on others ({len(waiting)}) " + "─" * 28)
        for item in waiting:
            print(f"  {item.received_at[:16]}  {item.subject[:52]}")
        if not waiting:
            print("  nothing older than 3 days without a reply")

        print("\n── voice profile " + "─" * 44)
        print(f"  {describe_voice(source.voice)}")

        # Sanity checks on the real data.
        if any(not i.received_at for i in items):
            problems.append("a message came back with no received time")
        if len({i.id for i in items}) != len(items):
            problems.append("two messages share an id, which would duplicate on resync")
        if any(i.sender_addr == ME and i.state != "waiting" for i in inbox):
            problems.append("your own mail appeared in the inbox list as needing a reply")

        target = next((i for i in inbox if i.state == "needs_me"), None)

        if "--thread" in sys.argv or "--draft" in sys.argv:
            if target is None:
                print("\nNothing needs a reply, so there is no thread to fetch.")
            else:
                print(f"\n── thread for: {target.subject[:50]} " + "─" * 10)
                t1 = time.perf_counter()
                thread = source.get_thread(target.id)
                print(f"  {len(thread.messages)} messages in "
                      f"{time.perf_counter() - t1:.1f} s")
                for m in thread.messages:
                    who = "me" if m["is_me"] else m["author"]
                    print(f"    [{who}, {m['sent_at'][:16]}] {m['body'][:90]}")
                if thread.messages:
                    store.set_thread(target.id, thread.messages)
                else:
                    problems.append("the thread fetch came back empty")

        if "--draft" in sys.argv and target is not None:
            print(f"\n── draft reply " + "─" * 46)
            t2 = time.perf_counter()
            try:
                draft = source.draft(target.id, "reply, keep it short")
                print(f"  to:      {draft.to_addr}")
                print(f"  subject: {draft.subject}")
                print(f"  body:\n{draft.body}")
                print(f"\n  cold draft: {time.perf_counter() - t2:.1f} s")
                if draft.to_addr != target.sender_addr:
                    problems.append("the draft is not addressed to the sender")
                if not draft.body.strip():
                    problems.append("the draft body came back empty")

                t3 = time.perf_counter()
                source.draft(target.id, "reply, even shorter")
                print(f"  warm draft: {time.perf_counter() - t3:.1f} s")
            except cli.ClaudeUnavailable as e:
                problems.append(f"drafting failed: {e}")
            finally:
                source.close()

            # The gate, exercised against the real source.
            from helm.connectors.base import SendRefused
            from helm.models import ApprovalToken
            try:
                source.send("whatever", ApprovalToken.mint_from_click("whatever"))
                problems.append("the real mail source accepted a send")
            except SendRefused as e:
                print(f"\n  send refused, correctly: {e}")

    print()
    for p in problems:
        print(f"PROBLEM: {p}")
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
