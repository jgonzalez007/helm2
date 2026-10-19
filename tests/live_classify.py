"""One real stage-2 pass over your actual inbox.

Sweeps mail, lets the rules do their work, then asks Claude about whatever is left
and shows what changed. Needs the connectors; takes several minutes.

    python -m tests.live_classify
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from helm import claude_cli as cli
from helm import classifier
from helm import flags as F
from helm.app import Api
from helm.bus import EventBus
from helm.store import Store

ME = "jose.gonzalez@sailpoint.com"
SECRET_MARKERS = ("preview", "body")


def main() -> int:
    print("checking connectors…")
    av = cli.probe_availability()
    print(f"  Microsoft 365: {'connected' if av.microsoft_365 else av.reason('Microsoft 365')}")
    if not av.microsoft_365:
        return 1

    problems: list[str] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "live.db")
        store.set_setting("internal_domains", ["sailpoint.com"])
        api = Api(store, EventBus(), demo=False, availability=av)

        print("\nsweeping mail (expect 3-4 minutes)…")
        t0 = time.perf_counter()
        api.poll("mail")
        state = {s["source"]: s for s in store.source_states()}.get("mail", {})
        print(f"  {time.perf_counter() - t0:.1f} s, {len(store.items())} rows, "
              f"health={state.get('health')}")
        if state.get("last_error"):
            print(f"  error: {state['last_error']}")
        if not store.items():
            print("\n  Nothing came back, so the classifier cannot be tested. The error "
                  "above says why; re-run tests.tune_mail to see the raw reply.")
            return 1

        cfg = api._settings_obj()
        items = store.items()
        flags = store.flags()
        batch = classifier.candidates(items, flags, cfg, store.sender_profiles())

        print(f"\n── the rules settled {len(items) - len(batch)} of {len(items)}; "
              f"asking about {len(batch)} " + "─" * 10)
        for item in batch[:classifier.BATCH]:
            print(f"  {item.sender_addr[:40]:42}{item.subject[:44]}")

        if not batch:
            print("\nNothing ambiguous. That is a valid outcome, but it means this run "
                  "did not exercise the classifier.")
            return 0

        # Prove, against real data, that no body text is in what we are about to send.
        rendered = classifier.render(batch[:classifier.BATCH])
        for item in batch[:classifier.BATCH]:
            body = (item.snippet or "").strip()
            if len(body) > 25 and body[:25] in rendered:
                problems.append(f"a message body leaked into the prompt: {item.id}")
        print(f"\n  prompt is {len(rendered)} chars, body-free check: "
              f"{'FAILED' if problems else 'ok'}")

        print("\nasking (expect 1-2 minutes)…")
        t1 = time.perf_counter()
        before = {i.id: store.flag(i.id).importance for i in batch}
        result = api.classify_ambiguous()
        print(f"  {time.perf_counter() - t1:.1f} s → {result}")

        if result.get("error"):
            problems.append(f"the classifier failed: {result['error']}")
            print("PASS" if not problems else "FAIL")
            return 1

        print("\n── verdicts " + "─" * 48)
        for item in batch[:classifier.BATCH]:
            flag = store.flag(item.id)
            profile = store.sender_profile(item.sender_addr) or {}
            moved = "" if flag.importance == before[item.id] else \
                f"  ({before[item.id]} → {flag.importance})"
            kind = "automated" if profile.get("is_automated") else "person"
            conf = f"{flag.confidence:.2f}" if flag.confidence is not None else " -- "
            print(f"  {flag.importance:7}{flag.urgency:7}{conf}  {kind:10}"
                  f"{item.sender_addr[:34]:36}{moved}")
            reason = next((r["detail"] for r in flag.reasons if r["rule"] == "claude"), "")
            if reason:
                print(f"          {reason}")

            if flag.decided_by == "claude" and flag.urgency not in ("now", "today",
                                                                   "week", "none"):
                problems.append(f"an invalid urgency survived: {flag.urgency}")

        # The property that matters: urgency must come from the rules, never stage 2.
        for item in batch[:classifier.BATCH]:
            flag = store.flag(item.id)
            rule_only = F.classify(item, cfg)
            if flag.urgency != rule_only.urgency:
                problems.append(
                    f"stage 2 changed urgency on {item.id}: "
                    f"{rule_only.urgency} → {flag.urgency}")
        print(f"\n  urgency unchanged by stage 2: {'FAILED' if problems else 'ok'}")

        learned = store.sender_profiles()
        print(f"\n── learned {len(learned)} senders " + "─" * 40)
        for addr, row in sorted(learned.items())[:15]:
            print(f"  {'auto' if row['is_automated'] else 'human'}  "
                  f"{row['confidence'] or 0:.2f}  {addr[:40]:42}{row['reason'][:50]}")

        # A second pass must cost nothing: everything is cached now.
        print("\nsecond pass, which should ask about nothing…")
        again = classifier.candidates(store.items(), store.flags(), api._settings_obj(),
                                      store.sender_profiles())
        print(f"  {len(again)} items still ambiguous")
        if len(again) >= len(batch):
            problems.append("the second pass did not benefit from the cache")

    print()
    for p in problems:
        print(f"PROBLEM: {p}")
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
