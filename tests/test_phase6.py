"""Phase 6: focus queue, privacy mode, waiting-on tracker.

No window, no network, no Windows API — `privacy.decide()` is pure so the rules can
be checked on any machine.

    python -m tests.test_phase6
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helm import flags as F
from helm import privacy as P
from helm.app import Api, _next_morning
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.connectors.mail_source import find_waiting
from helm.models import Item
from helm.store import Store

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


# ── privacy rules ───────────────────────────────────────────────────────────

def test_privacy_decide() -> None:
    print("privacy decides from the foreground window alone")
    apps = P.MEETING_APPS

    on, why = P.decide(P.Window(exe="ms-teams.exe", title="Weekly 1:1"), apps)
    check("Teams in front hides the panel", on)
    check("and says a meeting app, not that you are sharing",
          "meeting app" in why and "shar" not in why.replace("share is running", ""), why)

    on, why = P.decide(P.Window(exe="cpthost.exe", title=""), apps)
    check("Zoom's share host is a real share signal", on)
    check("and is described as one", "screen share" in why, why)

    check("a code editor does not", not P.decide(P.Window(exe="Code.exe"), apps)[0])
    check("nor does the browser", not P.decide(P.Window(exe="msedge.exe"), apps)[0])
    check("matching is case-insensitive",
          P.decide(P.Window(exe="MS-Teams.EXE"), apps)[0])

    # A window title is attacker-controllable: a meeting invite could be named
    # anything, so the title must never be what triggers hiding.
    check("a window titled like Teams does not trigger it",
          not P.decide(P.Window(exe="notepad.exe", title="Microsoft Teams"), apps)[0])
    check("nothing in front means nothing to hide from",
          not P.decide(None, apps)[0] and not P.decide(P.Window(), apps)[0])

    check("the app list is configurable",
          P.decide(P.Window(exe="mycorp-meet.exe"), {"mycorp-meet.exe"})[0])
    check("and an empty list disables matching",
          not P.decide(P.Window(exe="ms-teams.exe"), set())[0])


def test_privacy_manual_wins() -> None:
    print("the panic hotkey outranks the watcher")
    seen: list[dict] = []
    bus = EventBus()
    bus.subscribe("privacy_changed", seen.append)
    w = P.PrivacyWatcher(bus, enabled=lambda: True, apps=lambda: P.MEETING_APPS)

    check("starts visible", not w.state().on)
    w.set_manual(True)
    check("panic hides it", w.state().on)
    check("and names itself as the reason", "panic" in w.state().reason)
    check("the UI is told", len(seen) == 1 and seen[0]["on"] is True)

    # The watcher sees a calm desktop; manual must still hold.
    w._auto = P.PrivacyState(on=False)
    check("a calm desktop cannot un-hide a manual hide", w.state().on)

    w.set_manual(False)
    check("toggling back shows it", not w.state().on)
    check("every change is published", len(seen) == 2)

    w.set_manual()
    check("no argument toggles", w.state().on)

    check("a platform without a detector says so, rather than implying it watches",
          w.state().watching is False)


def test_privacy_disabled() -> None:
    print("turning automatic detection off really turns it off")
    bus = EventBus()
    w = P.PrivacyWatcher(bus, enabled=lambda: False, apps=lambda: P.MEETING_APPS)
    w._auto = P.PrivacyState(on=True, reason="a meeting app is in the foreground")
    # One loop body with the gate off clears the automatic state.
    w._stop.set()
    check("the state object still reports what it was told", w.state().on)
    w._auto = P.PrivacyState()
    check("and clears when the watcher clears it", not w.state().on)


def test_interruptions_are_gated() -> None:
    print("may_interrupt refuses, and says why")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        # An empty quiet window (start == end), so the other reasons test in isolation.
        store.set_setting("quiet_hours", {"start": "00:00", "end": "00:00"})
        api = Api(store, EventBus(), demo=True)

        check("a clear desktop may interrupt", api.may_interrupt()["ok"] is True)

        api.set_privacy(True)
        res = api.may_interrupt()
        check("privacy mode blocks it", res["ok"] is False)
        check("and the refusal names its reason", "panic" in res["reason"], res["reason"])
        api.set_privacy(False)

        api._privacy._last_window = P.Window(exe="game.exe", fullscreen=True)
        res = api.may_interrupt()
        check("a fullscreen app blocks it", res["ok"] is False)
        check("and says so", "fullscreen" in res["reason"], res["reason"])
        api._privacy._last_window = P.Window()

        store.set_setting("quiet_hours", {"start": "00:00", "end": "23:59"})
        res = api.may_interrupt()
        check("quiet hours block it", res["ok"] is False)
        check("and say so", "quiet" in res["reason"], res["reason"])


# ── snooze ──────────────────────────────────────────────────────────────────

def test_snooze_lands_at_a_predictable_hour() -> None:
    print("snooze lands tomorrow morning, not at a random hour")
    afternoon = datetime(2026, 10, 19, 14, 30).astimezone()
    target = _next_morning(afternoon)
    check("an afternoon snooze returns tomorrow at 08:00",
          (target.hour, target.minute) == (8, 0) and target.day == 20,
          target.isoformat())

    dawn = datetime(2026, 10, 19, 6, 10).astimezone()
    target = _next_morning(dawn)
    check("before 08:00 it returns this morning",
          (target.day, target.hour) == (19, 8), target.isoformat())

    midnight = datetime(2026, 10, 19, 0, 1).astimezone()
    check("just after midnight it is still the same morning",
          _next_morning(midnight).day == 19)

    check("it is always in the future", _next_morning(afternoon) > afternoon)


def test_snooze_comes_back() -> None:
    print("a snooze ends; it is not a delete")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        store.upsert_item(Item(id="a", source="mail", kind="email", subject="later"))
        store.upsert_item(Item(id="b", source="mail", kind="email", subject="much later"))

        past = iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        future = iso(datetime.now(timezone.utc) + timedelta(hours=5))
        store.set_item_state("a", "snoozed", snooze_until=past)
        store.set_item_state("b", "snoozed", snooze_until=future)

        woken = store.wake_snoozed()
        check("the one that is due wakes", woken == ["a"], str(woken))
        check("and is back in the queue", store.item("a").state == "needs_me")
        check("its snooze time is cleared", store.item("a").snooze_until == "")
        check("the one that is not due stays asleep", store.item("b").state == "snoozed")
        check("waking twice is a no-op", store.wake_snoozed() == [])

        # A snoozed row with no time set would otherwise wake instantly.
        store.set_item_state("b", "snoozed", snooze_until="")
        check("a snooze with no time never auto-wakes", store.wake_snoozed() == [])


def test_snooze_through_the_api() -> None:
    print("snooze through the bridge")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        api.set_item_state("demo-mail-1", "snoozed")
        item = store.item("demo-mail-1")
        check("state is snoozed", item.state == "snoozed")
        check("a wake time was set", bool(item.snooze_until))
        check("it is in the future",
              datetime.fromisoformat(item.snooze_until) > datetime.now().astimezone())
        check("at 08:00", datetime.fromisoformat(item.snooze_until).hour == 8)
        check("it leaves the needs-you list",
              "demo-mail-1" not in [i.id for i in store.items(states=("needs_me",))])

        # This was a real bug: a sweep rebuilds an Item from scratch, so every poll
        # used to overwrite `state` and silently undo Done and Snoozed.
        api.sync()
        after = store.item("demo-mail-1")
        check("a sweep does not undo a snooze", after.state == "snoozed", after.state)
        check("nor lose its wake time", bool(after.snooze_until))

        api.set_item_state("demo-mail-2", "done")
        api.sync()
        check("nor undo a Done", store.item("demo-mail-2").state == "done")

        # But a source may still move an open item between its own states.
        check("an item you have not touched still tracks its source",
              store.item("demo-mail-6").state == "waiting",
              store.item("demo-mail-6").state)


# ── waiting-on ──────────────────────────────────────────────────────────────

def test_waiting_resurfaces() -> None:
    print("waiting-on resurfaces with something to chase it")
    now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    sent = [
        {"id": "s1", "conversation_id": "c1", "subject": "SOW redlines",
         "to_emails": ["legal@northwind-example.com"],
         "sent_at": iso(now - timedelta(days=5)), "preview": "Redlines attached."},
        {"id": "s2", "conversation_id": "c2", "subject": "Lunch?",
         "to_emails": ["atlee.chalwell@sailpoint.com"],
         "sent_at": iso(now - timedelta(hours=6)), "preview": "Free at noon?"},
        # A note to yourself is not something anyone owes you an answer to.
        {"id": "s3", "conversation_id": "c3", "subject": "Reminder to me",
         "to_emails": ["jose.gonzalez@sailpoint.com"],
         "sent_at": iso(now - timedelta(days=9)), "preview": "Check the renewal."},
        # Outlook files RSVPs in Sent Items; they are not open questions either.
        {"id": "s4", "conversation_id": "c4", "subject": "Accepted: Weekly 1:1",
         "to_emails": ["scott.chen@sailpoint.com"],
         "sent_at": iso(now - timedelta(days=6)), "preview": ""},
    ]
    items = find_waiting(sent, [], "jose.gonzalez@sailpoint.com", now=now)
    ids = [i.id for i in items]
    check("five days with no reply surfaces", any("s1" in i for i in ids), str(ids))
    check("six hours does not", not any("s2" in i for i in ids), str(ids))
    check("a note to yourself does not", not any("s3" in i for i in ids), str(ids))
    check("an RSVP does not", not any("s4" in i for i in ids), str(ids))
    for item in items:
        check(f"{item.id} is marked waiting", item.state == "waiting")
        check(f"{item.id} records when you sent it", bool(item.sent_by_me_at))

    # The flag rules must agree that it is worth resurfacing at all.
    cfg = F.Settings(internal_domains={"sailpoint.com"})
    flag = F.classify(items[0], cfg, now=now)
    check("the waiting rule fires",
          any(r["rule"] == "waiting_on" for r in flag.reasons), str(flag.reasons))


def test_waiting_items_join_the_focus_queue() -> None:
    print("the focus queue includes what you are waiting on")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        rows = api.list_items()
        waiting = [r for r in rows if r["item"]["state"] == "waiting"]
        check("demo mode ships a waiting fixture", bool(waiting), str(len(rows)))

        # focusRows in ui/js/views.js keeps needs_me and waiting, minus calendar.
        queue = [r for r in rows
                 if r["item"]["state"] in ("needs_me", "waiting")
                 and r["item"]["source"] != "calendar"]
        check("it reaches the queue",
              any(r["item"]["state"] == "waiting" for r in queue))
        check("but sorts behind anything important and urgent",
              queue[0]["flag"]["quadrant"] == "important_urgent",
              queue[0]["flag"]["quadrant"])
        check("no calendar event is ever in the focus queue",
              not any(r["item"]["source"] == "calendar" for r in queue))

        # "draft a follow-up" has to reach the drafting path, or the button lies.
        from helm.app import _WRITE_INTENTS
        prompt = "draft a follow-up — no reply since I sent this"
        check("the follow-up prompt counts as a write intent",
              any(w in prompt.lower() for w in _WRITE_INTENTS))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
