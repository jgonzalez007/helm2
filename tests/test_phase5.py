"""Phase 5 self-check: stage-2 classification, caching, learning, and its limits.

The two properties worth breaking a build over:

  * stage 2 never sets urgency
  * no message body reaches stage 2

    python -m tests.test_phase5
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helm import claude_cli as cli
from helm import classifier
from helm import flags as F
from helm.app import Api
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.models import Flag, Item
from helm.store import Store

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
ME = "jose.gonzalez@sailpoint.com"
SECRET = "LAUNCH-CODE-NEVER-SEND-THIS"


def mail(**kw) -> Item:
    base = dict(id="m1", source="mail", kind="email", subject="Following up on Nexus",
                sender="Stranger", sender_addr="stranger@unknown-corp.example",
                sender_external=True, snippet=f"Body text containing {SECRET}.",
                received_at=NOW.isoformat(timespec="seconds"))
    base.update(kw)
    return Item(**base)


# ── The two properties that matter ──────────────────────────────────────────

def test_no_body_reaches_stage_two() -> None:
    print("stage 2 never sees a message body")
    item = mail()
    p = classifier.payload(item)

    check("the payload has only the named fields",
          set(p) == set(classifier.PAYLOAD_FIELDS),
          f"extra={sorted(set(p) - set(classifier.PAYLOAD_FIELDS))}")
    check("'snippet' is not a payload field", "snippet" not in classifier.PAYLOAD_FIELDS)
    check("no payload value contains the body",
          not any(SECRET in str(v) for v in p.values()), str(p))

    rendered = classifier.render([item])
    check("the rendered prompt does not contain the body", SECRET not in rendered,
          rendered)
    check("but it does carry the subject", "Nexus" in rendered)
    check("and the sender", "stranger@unknown-corp.example" in rendered)

    full = classifier.PROMPT.format(items=rendered)
    check("the whole prompt is body-free", SECRET not in full)
    check("the prompt tells the model it gets no body",
          "do not get the message body" in full)
    check("the prompt forbids judging urgency",
          "Do NOT judge urgency" in full)


def test_stage_two_cannot_set_urgency() -> None:
    print("stage 2 never sets urgency")
    for claimed in ("now", "today", "week", "none", "immediately", None):
        flag = Flag(item_id="m1", importance="normal", urgency="week",
                    reasons=[{"rule": "deadline", "detail": "deadline in 4 d"}])
        verdict = {"id": "m1", "importance": "high", "is_automated": False,
                   "expects_reply": True, "confidence": 0.8, "reason": "a real request",
                   "urgency": claimed}
        out = classifier.apply(verdict, flag)
        check(f"a verdict claiming urgency={claimed!r} does not move it",
              out.urgency == "week", out.urgency)

    check("importance did move", out.importance == "high")
    check("it is attributed to Claude", out.decided_by == "claude")
    check("with a confidence", out.confidence == 0.8)
    check("the rule reason survives",
          any(r["rule"] == "deadline" for r in out.reasons))
    check("and Claude's reason is added",
          any(r["rule"] == "claude" for r in out.reasons))

    # Re-applying must not stack duplicate reasons.
    classifier.apply(verdict, out)
    check("re-running does not duplicate the reason",
          len([r for r in out.reasons if r["rule"] == "claude"]) == 1)


def test_stage_two_never_overrides_a_click() -> None:
    print("a pinned flag outranks any model")
    flag = Flag(item_id="m1", importance="low", urgency="none", pinned=True)
    verdict = {"id": "m1", "importance": "high", "is_automated": False,
               "expects_reply": True, "confidence": 0.99, "reason": "looks important"}
    out = classifier.apply(verdict, flag)
    check("importance is untouched", out.importance == "low")
    check("it is still attributed to the user", out.decided_by != "claude")
    check("and no reason was added", not any(r["rule"] == "claude" for r in out.reasons))


# ── Which items get asked about ─────────────────────────────────────────────

def test_candidates() -> None:
    print("only the genuinely unclear items are sent")
    cfg = F.Settings(managers={"scott.chen@sailpoint.com"},
                     internal_domains={"sailpoint.com"})

    unclear = mail(id="unclear")
    settled = mail(id="settled", sender_addr="scott.chen@sailpoint.com",
                   sender_external=False)
    picked = {i.id for i in classifier.candidates([unclear, settled], {}, cfg, {}, NOW)}
    check("an unknown sender with no signal is asked about", "unclear" in picked)
    check("a manager is settled by rules and not asked", "settled" not in picked,
          str(picked))

    pinned = {"pinned": Flag(item_id="pinned", pinned=True)}
    check("a pinned flag is never asked about",
          classifier.candidates([mail(id="pinned")], pinned, cfg, {}, NOW) == [])

    known = {"stranger@unknown-corp.example": {"is_automated": True}}
    check("a sender already learned is not asked again",
          classifier.candidates([unclear], {}, cfg, known, NOW) == [])

    fresh = {"unclear": Flag(item_id="unclear", decided_by="claude",
                             recheck_after=(NOW + timedelta(hours=10))
                             .isoformat(timespec="seconds"))}
    check("a cached verdict is not re-asked",
          classifier.candidates([unclear], fresh, cfg, {}, NOW) == [])

    stale = {"unclear": Flag(item_id="unclear", decided_by="claude",
                             recheck_after=(NOW - timedelta(hours=1))
                             .isoformat(timespec="seconds"))}
    check("an expired verdict is re-asked",
          len(classifier.candidates([unclear], stale, cfg, {}, NOW)) == 1)

    no_recheck = {"unclear": Flag(item_id="unclear", decided_by="claude",
                                  recheck_after="")}
    check("a verdict with no expiry is treated as stale",
          len(classifier.candidates([unclear], no_recheck, cfg, {}, NOW)) == 1)


def test_verdicts_are_sanitised() -> None:
    print("a verdict is coerced before it is trusted")
    check("a non-dict is dropped", classifier._clean_verdict("nope") is None)
    check("no id means dropped", classifier._clean_verdict({"importance": "high"}) is None)

    v = classifier._clean_verdict({"id": "x", "importance": "CRITICAL",
                                   "confidence": "very sure", "reason": "y" * 500})
    check("an invalid importance falls back to normal", v["importance"] == "normal")
    check("an unparsable confidence falls back to 0.5", v["confidence"] == 0.5)
    check("the reason is capped", len(v["reason"]) <= 140)

    for raw, expected in (({"id": "x", "confidence": 9}, 1.0),
                          ({"id": "x", "confidence": -4}, 0.0),
                          ({"id": "x", "confidence": 0.33}, 0.33)):
        check(f"confidence {raw['confidence']} clamps to {expected}",
              classifier._clean_verdict(raw)["confidence"] == expected)

    check("importance is lowercased",
          classifier._clean_verdict({"id": "x", "importance": "HIGH"})["importance"] == "high")


def test_batch_drops_unknown_ids() -> None:
    print("a verdict about an item we did not ask about is discarded")
    asked = [mail(id="a"), mail(id="b")]
    real = cli.run_once
    cli.run_once = lambda *_a, **_k: (
        '[{"id":"a","importance":"high","is_automated":false,"expects_reply":true,'
        '"confidence":0.9,"reason":"real request"},'
        '{"id":"ghost","importance":"high","is_automated":false,"expects_reply":true,'
        '"confidence":0.9,"reason":"made up"}]', 100)
    try:
        out = classifier.classify_batch(asked)
    finally:
        cli.run_once = real
    check("only the asked-about id survives", [v["id"] for v in out] == ["a"], str(out))


def test_batch_uses_no_tools() -> None:
    print("stage 2 is a judgement, not a lookup")
    seen: dict = {}
    real = cli.run_once

    def fake(prompt, allowed_tools, timeout=None):
        seen["tools"] = allowed_tools
        return '[{"id":"a","importance":"low","is_automated":true,'\
               '"expects_reply":false,"confidence":0.9,"reason":"digest"}]', 100

    cli.run_once = fake
    try:
        classifier.classify_batch([mail(id="a")])
    finally:
        cli.run_once = real
    check("it is given no tools at all", seen["tools"] == (), str(seen["tools"]))

    bad = cli.run_once
    cli.run_once = lambda *_a, **_k: ("I could not do that.", 100)
    try:
        classifier.classify_batch([mail(id="a")])
    except cli.ClaudeUnavailable as e:
        check("an unreadable reply raises rather than returning nothing",
              "could not read the verdicts" in str(e))
    else:
        raise AssertionError("FAILED: an unreadable reply was accepted")
    finally:
        cli.run_once = bad


# ── Learned senders ─────────────────────────────────────────────────────────

def test_learned_senders_apply_for_free() -> None:
    print("what HELM learned is applied by the rules, with no call")
    cfg = F.Settings(
        internal_domains={"sailpoint.com"},
        sender_profiles={"health@aws.com": {
            "is_automated": True, "expects_reply": False, "importance_hint": "low",
            "reason": "AWS health notifications", "pinned": False}})

    item = mail(sender_addr="health@aws.com")
    flag = F.classify(item, cfg, now=NOW)
    check("a known automated sender is low", flag.importance == "low", flag.importance)
    check("the reason names what was learned",
          any("AWS health" in r["detail"] for r in flag.reasons), str(flag.reasons))
    check("and it is still attributed to the rules", flag.decided_by == "rules")

    sig = F._signals(item, cfg.normalized(), NOW)
    _, _, _, confident = F._score(sig)
    check("so stage 2 is not asked again", not F.needs_claude(sig, confident))


def test_a_click_outranks_a_verdict() -> None:
    print("a click is remembered and no classifier overwrites it")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")

        store.set_sender_profile("vendor@x.com", is_automated=False,
                                 importance_hint="high", reason="model said so",
                                 decided_by="claude", confidence=0.8)
        check("a model verdict is stored",
              store.sender_profile("vendor@x.com")["decided_by"] == "claude")

        store.set_sender_profile("vendor@x.com", is_automated=True, expects_reply=False,
                                 importance_hint="low", reason="you said not important",
                                 decided_by="click", pinned=True)
        pinned = store.sender_profile("vendor@x.com")
        check("a click replaces it", pinned["importance_hint"] == "low")
        check("and pins it", pinned["pinned"] == 1)

        store.set_sender_profile("vendor@x.com", is_automated=False,
                                 importance_hint="high", reason="model changed its mind",
                                 decided_by="claude", confidence=0.95)
        after = store.sender_profile("vendor@x.com")
        check("a later verdict cannot overwrite the click",
              after["importance_hint"] == "low", after["importance_hint"])
        check("and the click's reason stands",
              "you said" in after["reason"], after["reason"])

        store.forget_sender("vendor@x.com")
        check("forgetting removes it", store.sender_profile("vendor@x.com") is None)


def test_clicking_not_important_teaches_durably() -> None:
    print("one click changes what happens to that sender's next message")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        item = store.item("demo-mail-2")        # Bryan Kelley, a VIP in the fixtures
        check("it starts important",
              store.flag(item.id).importance == "high",
              store.flag(item.id).importance)

        api.flag_override(item.id, "not_important")
        profile = store.sender_profile(item.sender_addr)
        check("the sender is now remembered", profile is not None)
        check("as one you do not want", profile["importance_hint"] == "low")
        check("pinned, because you said it", profile["pinned"] == 1)
        check("with a reason you would recognise",
              "not important" in profile["reason"], profile["reason"])

        # A brand-new message from the same person inherits the lesson.
        fresh = Item(id="new-from-bryan", source="mail", kind="email",
                     subject="Another thing entirely",
                     sender=item.sender, sender_addr=item.sender_addr,
                     received_at=NOW.isoformat(timespec="seconds"))
        flag = F.classify(fresh, api._settings_obj(), now=NOW)
        check("their next message is low too", flag.importance == "low", flag.importance)
        check("even though they are still on the VIP list",
              item.sender_addr in (store.get_setting("vips") or []),
              str(store.get_setting("vips")))

        check("keyword weights moved as well",
              any(v < 0 for v in (store.get_setting("keyword_weights") or {}).values()),
              str(store.get_setting("keyword_weights")))


def test_more_important_teaches_the_other_way() -> None:
    print("raising importance also sticks")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()
        api.flag_override("demo-mail-4", "more_important")   # the newsletter
        profile = store.sender_profile("no-reply@sailpoint.com")
        check("the sender is remembered as a person you want",
              profile["importance_hint"] == "high", str(profile))
        check("and it is pinned", profile["pinned"] == 1)

        check("urgency overrides do not touch the sender profile",
              api.flag_override("demo-mail-3", "more_urgent") is not None)
        check("ap-invoices was not profiled by an urgency click",
              store.sender_profile("ap-invoices@sailpoint.com") is None,
              str(store.sender_profile("ap-invoices@sailpoint.com")))


def test_needs_reply_uses_learned_senders() -> None:
    print("needs-reply asks what HELM learned before it guesses from the address")
    from helm.connectors import mail_source as ms

    item = ms._clean_message(
        {"id": "x", "subject": "Ticket assigned", "from_email": "helpdesk@acme.example",
         "from_name": "Helpdesk", "to_emails": [ME], "cc_emails": [],
         "received": "2026-10-19T08:00:00Z", "preview": "A ticket."}, ME,
        ["sailpoint.com"])

    wanted, reason = ms.needs_reply(item, set(), {})
    check("an unknown address with no pattern match does need a reply", wanted is True)

    learned = {"helpdesk@acme.example": {"is_automated": True, "expects_reply": False,
                                         "reason": "a ticketing workflow"}}
    wanted, reason = ms.needs_reply(item, set(), learned)
    check("once learned, it does not", wanted is False)
    check("and the reason is the learned one", reason == "a ticketing workflow", reason)

    pinned_yes = {"helpdesk@acme.example": {"is_automated": True, "expects_reply": True,
                                            "pinned": True, "reason": "you answer these"}}
    wanted, reason = ms.needs_reply(item, set(), pinned_yes)
    check("your click wins over the automated verdict", wanted is True)
    check("with your reason", "you answer these" in reason, reason)


def test_demo_mode_does_not_call_out() -> None:
    print("demo mode never reaches a connector")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        called = {"n": 0}
        real = cli.run_once

        def tripwire(*_a, **_k):
            called["n"] += 1
            raise AssertionError("demo mode called the connector")

        cli.run_once = tripwire
        try:
            out = api.classify_ambiguous()
        finally:
            cli.run_once = real
        check("it skips with a reason", "skipped" in out, str(out))
        check("and made no call", called["n"] == 0)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
