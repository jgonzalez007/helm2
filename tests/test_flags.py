"""Self-check for the rules that must not break. No test runner, no network, no window.

    python -m tests.test_flags
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helm import flags as F
from helm.connectors.base import SendRefused
from helm.connectors.demo import DEMO_SETTINGS, DemoSource
from helm.models import ApprovalToken, Flag, Item
from helm.store import Store

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)   # a Monday
CFG = F.Settings(
    vips={"heidi.stark@sailpoint.com"},
    managers={"scott.chen@sailpoint.com"},
    internal_domains={"sailpoint.com"},
)

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def mail(**kw) -> Item:
    base = dict(id="x", source="mail", kind="email", subject="hello",
                sender_addr="someone@sailpoint.com", received_at=iso(NOW))
    base.update(kw)
    return Item(**base)


# ── the rule the whole design rests on ──────────────────────────────────────

def test_content_cannot_flag_itself() -> None:
    print("content cannot influence its own flag")

    quiet = mail(id="a", subject="Re: PO 88213", snippet="The purchase order needs a look.")
    loud = mail(id="a", subject="Re: PO 88213",
                snippet="URGENT! ACTION REQUIRED! Respond IMMEDIATELY or face suspension. "
                        "This is HIGH PRIORITY and TIME SENSITIVE.")

    fq = F.classify(quiet, CFG, now=NOW)
    fl = F.classify(loud, CFG, now=NOW)
    check("identical flags regardless of body urgency words",
          (fq.importance, fq.urgency) == (fl.importance, fl.urgency),
          f"{fq.importance}/{fq.urgency} vs {fl.importance}/{fl.urgency}")
    check("loud internal message is still not urgent", fl.urgency == "none", fl.urgency)

    # Subject-line shouting is equally inert; only a *date* in the subject counts.
    shouty = mail(id="a", subject="URGENT ACTION REQUIRED HIGH PRIORITY")
    fs = F.classify(shouty, CFG, now=NOW)
    check("shouting in the subject sets no urgency", fs.urgency == "none", fs.urgency)

    # And the scoring path provably cannot reach a body.
    sig = F._signals(loud, CFG.normalized(), NOW)
    check("signal dict carries no body text",
          not any(isinstance(v, str) and "URGENT" in v.upper() for v in sig.values()))


def test_phishing_fixture() -> None:
    print("phishing-style external message")

    phish = mail(
        id="p", subject="URGENT: action required — verify your account within 24 hours",
        sender="IT Helpdesk", sender_addr="it-support@helpdesk-verify.net",
        sender_external=True,
        snippet="Your mailbox will be suspended immediately. Click here to verify your account.",
    )
    f = F.classify(phish, CFG, now=NOW)
    check("flags stay quiet", (f.importance, f.urgency) == ("normal", "none"),
          f"{f.importance}/{f.urgency}")
    check("quadrant is 'neither'", f.quadrant == "neither", f.quadrant)
    check("posture warning is raised", bool(f.posture_warning))
    check("warning names the real address", "helpdesk-verify.net" in f.posture_warning)
    check("warning says the words had no effect", "did not raise" in f.posture_warning)

    # Same words from a known internal sender: no warning, and still no urgency.
    internal = mail(id="q", subject="urgent: please respond",
                    sender_addr="scott.chen@sailpoint.com")
    fi = F.classify(internal, CFG, now=NOW)
    check("no posture warning for a known sender", fi.posture_warning == "")
    check("known sender is important but not urgent",
          (fi.importance, fi.urgency) == ("high", "none"), f"{fi.importance}/{fi.urgency}")


# ── importance ──────────────────────────────────────────────────────────────

def test_importance() -> None:
    print("importance rules")
    check("manager → high",
          F.classify(mail(sender_addr="scott.chen@sailpoint.com"), CFG, now=NOW).importance == "high")
    check("VIP → high",
          F.classify(mail(sender_addr="heidi.stark@sailpoint.com"), CFG, now=NOW).importance == "high")
    check("no-reply → low",
          F.classify(mail(sender_addr="no-reply@sailpoint.com"), CFG, now=NOW).importance == "low")
    check("DM → high",
          F.classify(mail(kind="dm", source="slack"), CFG, now=NOW).importance == "high")
    check("Cc only → normal",
          F.classify(mail(to_me_direct=False), CFG, now=NOW).importance == "normal")

    muted = F.Settings(muted_senders={"noisy@sailpoint.com"}, internal_domains={"sailpoint.com"})
    check("muted sender → low",
          F.classify(mail(sender_addr="noisy@sailpoint.com"), muted, now=NOW).importance == "low")


def test_meeting_importance() -> None:
    print("meeting importance")
    ev = Item(id="m", source="calendar", kind="event", subject="Deep dive",
              starts_at=iso(NOW + timedelta(hours=4)), external_attendees=True)
    check("external attendees → high", F.classify(ev, CFG, now=NOW).importance == "high")

    ev2 = Item(id="m2", source="calendar", kind="event", subject="Internal sync",
               starts_at=iso(NOW + timedelta(hours=4)), presenter_is_me=True)
    check("you present → high", F.classify(ev2, CFG, now=NOW).importance == "high")


# ── urgency and time awareness ──────────────────────────────────────────────

def test_urgency_escalates_with_time() -> None:
    print("urgency is time-aware")
    deadline = NOW + timedelta(days=5)
    item = mail(deadline_at=iso(deadline))

    seen = []
    for offset_days in (0, 3, 4.5):
        at = NOW + timedelta(days=offset_days)
        seen.append(F.classify(item, CFG, now=at).urgency)
    check("week → today → now as the deadline nears", seen == ["week", "today", "now"], str(seen))

    far = mail(deadline_at=iso(NOW + timedelta(days=30)))
    check("a distant deadline is not urgent", F.classify(far, CFG, now=NOW).urgency == "none")


def test_meeting_urgency() -> None:
    print("meeting urgency")
    soon = Item(id="s", source="calendar", kind="event", subject="1:1",
                starts_at=iso(NOW + timedelta(minutes=10)))
    check("starts in 10 min → now", F.classify(soon, CFG, now=NOW).urgency == "now")

    pending = Item(id="r", source="calendar", kind="event", subject="Review",
                   starts_at=iso(NOW + timedelta(hours=6)), rsvp_pending=True)
    check("RSVP pending under 24 h → today", F.classify(pending, CFG, now=NOW).urgency == "today")

    no_agenda = Item(id="n", source="calendar", kind="event", subject="Review",
                     starts_at=iso(NOW + timedelta(hours=6)), has_agenda=False)
    check("no agenda under 24 h → today", F.classify(no_agenda, CFG, now=NOW).urgency == "today")

    clash = Item(id="c", source="calendar", kind="event", subject="Clash",
                 starts_at=iso(NOW + timedelta(hours=6)), conflicts_with=["other"])
    check("conflict under 24 h → today", F.classify(clash, CFG, now=NOW).urgency == "today")


def test_subject_deadline_parsing() -> None:
    print("deadline parsing from the subject line")
    # Monday 09:00 → Thursday 17:00 is ~80 h, past the 72 h "today" threshold.
    thu = F.classify(mail(subject="Sign-off needed by Thursday"), CFG, now=NOW)
    check("'by Thursday' from a Monday → this week", thu.urgency == "week", thu.urgency)
    check("reason names the deadline",
          any(r["rule"] == "deadline" for r in thu.reasons))

    wed = F.classify(mail(subject="Sign-off needed by Wednesday"), CFG, now=NOW)
    check("'by Wednesday' from a Monday → today", wed.urgency == "today", wed.urgency)

    # Tomorrow 17:00 from 09:00 is 32 h out: act on it today, not this second.
    tom = F.classify(mail(subject="Numbers due tomorrow"), CFG, now=NOW)
    check("'tomorrow' → today", tom.urgency == "today", tom.urgency)

    eod = F.classify(mail(subject="Numbers due today EOD"), CFG, now=NOW)
    check("'today EOD' → now", eod.urgency == "now", eod.urgency)

    none = F.classify(mail(subject="Catch-up when you get a chance"), CFG, now=NOW)
    check("no date → no urgency", none.urgency == "none", none.urgency)


def test_waiting_on() -> None:
    print("waiting-on tracker")
    sent = mail(sent_by_me_at=iso(NOW - timedelta(days=5)), state="waiting")
    f = F.classify(sent, CFG, now=NOW)
    check("5 days with no reply surfaces", f.urgency == "week", f.urgency)
    check("reason explains the wait", any(r["rule"] == "waiting_on" for r in f.reasons))

    fresh = mail(sent_by_me_at=iso(NOW - timedelta(hours=6)), state="waiting")
    check("6 hours does not", F.classify(fresh, CFG, now=NOW).urgency == "none")

    answered = mail(sent_by_me_at=iso(NOW - timedelta(days=9)),
                    last_reply_at=iso(NOW - timedelta(days=8)), state="waiting")
    check("a reply clears it", F.classify(answered, CFG, now=NOW).urgency == "none")


# ── quadrants, overrides, learning ──────────────────────────────────────────

def test_quadrants() -> None:
    print("quadrants")
    cases = {
        "important_urgent": Flag(item_id="1", importance="high", urgency="now"),
        "important_only": Flag(item_id="2", importance="high", urgency="week"),
        "urgent_only": Flag(item_id="3", importance="normal", urgency="today"),
        "neither": Flag(item_id="4", importance="low", urgency="none"),
    }
    for expected, flag in cases.items():
        check(f"{expected}", flag.quadrant == expected, flag.quadrant)

    ranks = [F.QUADRANT_RANK[q] for q in
             ("important_urgent", "important_only", "urgent_only", "neither")]
    check("focus order is strictly descending in priority", ranks == sorted(ranks))


def test_override_is_pinned_forever() -> None:
    print("overrides are pinned and never recomputed")
    item = mail(sender_addr="scott.chen@sailpoint.com")   # a manager → high
    f = F.classify(item, CFG, now=NOW)
    check("starts high", f.importance == "high")

    f = F.override(f, "not_important")
    check("override applies", f.importance == "low")
    check("override is pinned", f.pinned is True)
    check("reason records the override", any(r["rule"] == "override" for r in f.reasons))

    again = F.classify(item, CFG, now=NOW, existing=f)
    check("reclassifying returns the pinned flag untouched", again.importance == "low")
    check("same object, not a recomputation", again is f)

    g = F.override(Flag(item_id="z", urgency="none"), "more_urgent")
    check("more_urgent steps none → week", g.urgency == "week", g.urgency)
    g = F.override(g, "more_urgent")
    check("and week → today", g.urgency == "today", g.urgency)


def test_needs_claude() -> None:
    print("which cases go to Claude")
    unknown = mail(sender_addr="stranger@unknown-corp.example",
                   subject="Following up on Nexus", sender_external=True)
    sig = F._signals(unknown, CFG.normalized(), NOW)
    _, _, _, confident = F._score(sig)
    check("unknown sender, no date, no keyword → stage 2", F.needs_claude(sig, confident))

    known = mail(sender_addr="scott.chen@sailpoint.com")
    sig2 = F._signals(known, CFG.normalized(), NOW)
    _, _, _, confident2 = F._score(sig2)
    check("manager → settled by rules alone", not F.needs_claude(sig2, confident2))


# ── the send gate ───────────────────────────────────────────────────────────

def test_send_requires_a_click() -> None:
    print("nothing sends without a click")
    src = DemoSource("mail")
    draft = src.draft("demo-mail-1", "draft reply")

    for bad in (None, "token", 42, {"draft_id": draft.id}):
        try:
            src.send(draft.id, bad)
        except SendRefused:
            pass
        else:
            raise AssertionError(f"FAILED: send accepted {bad!r} as approval")
    check("send refuses anything that is not a minted token")

    other = ApprovalToken.mint_from_click("some-other-draft")
    try:
        src.send(draft.id, other)
    except SendRefused:
        check("a token minted for another draft is refused")
    else:
        raise AssertionError("FAILED: cross-draft token accepted")

    good = ApprovalToken.mint_from_click(draft.id)
    check("a clicked token works", src.send(draft.id, good).get("ok") is True)
    check("demo mode still sends nothing", src.send(draft.id, good).get("demo") is True)


def test_action_log_rejects_fake_approval() -> None:
    print("the action log cannot record a fake approval")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = Store(Path(d) / "t.db")
        s.log_action("send", "a@b.com", "mail", "body", "sent", approved_by="click")
        check("a real click is recorded", s.action_log()[0]["approved_by"] == "click")
        try:
            s.log_action("send", "a@b.com", "mail", "body", "sent", approved_by="auto")
        except ValueError:
            check("anything other than 'click' raises instead of writing")
        else:
            raise AssertionError("FAILED: action log accepted approved_by='auto'")


def test_shortcuts_are_links_only() -> None:
    print("shortcuts are http/https only")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = Store(Path(d) / "t.db")
        s.add_shortcut("Jira", "https://example.atlassian.net")
        check("https is accepted", len(s.shortcuts()) == 1)
        for bad in ("file:///C:/Windows/System32/cmd.exe", "cmd.exe /c del",
                    "javascript:alert(1)", "C:\\tool.bat", "ftp://host/x"):
            try:
                s.add_shortcut("bad", bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"FAILED: shortcut accepted {bad!r}")
        check("files, commands and other schemes are refused")


# ── fixtures cover every quadrant ───────────────────────────────────────────

def test_demo_covers_all_quadrants() -> None:
    print("demo fixtures cover all four quadrants")
    # Exactly what demo mode seeds, so this checks the shipped experience.
    cfg = F.Settings(managers=set(DEMO_SETTINGS["managers"]),
                     vips=set(DEMO_SETTINGS["vips"]),
                     internal_domains=set(DEMO_SETTINGS["internal_domains"]))
    found = set()
    phish_seen = False
    for name in ("mail", "slack", "calendar"):
        for item in DemoSource(name).list_new(""):
            f = F.classify(item, cfg)
            found.add(f.quadrant)
            if f.posture_warning:
                phish_seen = True
                check("the phishing fixture is not urgent", f.urgency == "none", f.urgency)
    for q in ("important_urgent", "important_only", "urgent_only", "neither"):
        check(f"fixture exists for {q}", q in found, str(sorted(found)))
    check("a phishing-style fixture is present", phish_seen)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
