"""One real Slack sweep through the approved connector. Read and draft only.

Nothing is posted and nothing can be: SlackSource.send refuses until phase 10.

    python -m tests.live_slack            sweep only
    python -m tests.live_slack --draft    sweep, then draft a reply
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from helm import claude_cli as cli
from helm.connectors.base import SendRefused
from helm.connectors.slack_source import SlackSource
from helm.flags import Settings, classify
from helm.models import ApprovalToken
from helm.store import Store

ME = "jose.gonzalez@sailpoint.com"


def main() -> int:
    print("checking connectors…")
    av = cli.probe_availability()
    print(f"  Slack: {'connected' if av.slack else av.reason('Slack')}")
    if not av.slack:
        return 1

    problems: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "live.db")
        source = SlackSource(ME, ["sailpoint.com"], av, store=store)

        print("\nsweeping Slack (three tool calls, expect 3-6 minutes)…")
        t0 = time.perf_counter()
        items = source.list_new("")
        elapsed = time.perf_counter() - t0
        st = source.status()

        print(f"\nstatus:     {st.health}")
        print(f"round-trip: {elapsed:.1f} s")
        if st.last_error:
            print(f"error:      {st.last_error}")
        print(f"me:         {source.my_handle or '?'} / {source.my_id or '?'}")
        print(f"rows:       {len(items)}")

        if st.health != "live":
            print("\nThe sweep did not succeed. Nothing below is real.")
            return 1
        if not items:
            print("\nThe sweep succeeded but returned nothing. Either Slack really is "
                  "quiet for this window, or the prompt is not getting data. Re-run "
                  "tests.tune_slack to see the raw shapes.")
            return 1
        if not source.my_id:
            problems.append("the sweep did not learn your Slack user id, so a direct "
                            "mention cannot be told from a broadcast")

        cfg = Settings(internal_domains={"sailpoint.com"})
        needs = [i for i in items if i.state == "needs_me"]
        rest = [i for i in items if i.state != "needs_me"]

        print(f"\n── waiting on you ({len(needs)}) " + "─" * 34)
        for item in needs:
            store.upsert_item(item)
            flag = classify(item, cfg)
            print(f"  {item.received_at[:16]}  {item.kind:8}{item.subject[:28]:30}"
                  f"{flag.importance}/{flag.urgency}")
            print(f"        {item.sender}: {item.snippet[:72]}")
            print(f"        {source.reply_reason(item.id)}")

        print(f"\n── seen, not waiting on you ({len(rest)}) " + "─" * 22)
        for item in rest[:10]:
            print(f"  {item.kind:8}{item.subject[:28]:30}"
                  f"{source.reply_reason(item.id)[:44]}")

        # Checks against the real data.
        if any(not i.received_at for i in items):
            problems.append("a message came back with no timestamp")
        if len({i.id for i in items}) != len(items):
            problems.append("two messages share an id, which would duplicate on resync")
        for item in items:
            if "<@" in item.snippet or "<!" in item.snippet or "<http" in item.snippet:
                problems.append(f"raw Slack markup reached the display: {item.snippet[:60]}")
                break
        broadcasts = [i for i in items
                      if i.kind != "dm" and not i.to_me_direct and i.state == "needs_me"]
        if broadcasts:
            problems.append(f"{len(broadcasts)} channel messages that do not name you "
                            "are queued as needing a reply")

        target = next((i for i in needs if i.kind in ("dm", "mention")), None)

        if "--draft" in sys.argv and target is not None:
            print(f"\n── draft reply to {target.subject} " + "─" * 24)
            t1 = time.perf_counter()
            try:
                draft = source.draft(target.id, "reply, keep it short")
                print(f"  to:   {draft.to_addr}")
                print(f"  body: {draft.body}")
                print(f"\n  cold draft: {time.perf_counter() - t1:.1f} s")
                if draft.to_addr != target.subject:
                    problems.append("the draft is not addressed to the conversation")
                if not draft.body.strip():
                    problems.append("the draft body came back empty")
                if draft.subject:
                    problems.append("a Slack draft should carry no subject line")
            except cli.ClaudeUnavailable as e:
                problems.append(f"drafting failed: {e}")
            finally:
                source.close()

            try:
                source.send("whatever", ApprovalToken.mint_from_click("whatever"))
                problems.append("the real Slack source accepted a post")
            except SendRefused as e:
                print(f"\n  post refused, correctly: {e}")
        elif "--draft" in sys.argv:
            print("\nNothing is waiting on you, so there is no reply to draft.")

    print()
    for p in problems:
        print(f"PROBLEM: {p}")
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
