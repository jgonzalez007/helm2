"""Phase 8: daily summaries, weekly metrics and commitments.

Everything here is computed from rows already in the store, so these checks need no
window, no network and no model call.

    python -m tests.test_phase8
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from helm import summaries as S
from helm.app import Api
from helm.bus import EventBus
from helm.connectors.demo import DEMO_SETTINGS
from helm.models import Item
from helm.store import Store

passed = 0
MON = datetime(2026, 10, 19, 9, 0).astimezone()    # a Monday


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hhmm(iso: str) -> str:
    """The clock time out of an ISO string — it ends with a timezone offset."""
    return datetime.fromisoformat(iso).strftime("%H:%M")


def texts(commitments) -> list[str]:
    return [c.text.lower() for c in commitments]


# ── commitments ─────────────────────────────────────────────────────────────

def test_only_your_own_promises_count() -> None:
    print("a commitment is something you said you would do")
    found = S.extract_commitments([
        {"body": "I'll send the Q4 numbers by Wednesday.", "to": "scott@x.com"},
        {"body": "I will review the SOW tonight."},
        {"body": "Let me pull the renewal dates together."},
        {"body": "We'll get you the security pack."},
    ], now=MON)
    check("'I'll send' is a commitment", any("i'll send" in t for t in texts(found)),
          str(texts(found)))
    check("'I will review' too", any("i will review" in t for t in texts(found)))
    check("'Let me pull' too", any("let me pull" in t for t in texts(found)))
    check("'We'll get' too", any("we'll get" in t for t in texts(found)))
    check("four messages, four promises", len(found) == 4, str(len(found)))

    # Real sent mail drops the pronoun. Safe because the input is already only
    # what you wrote, so a sentence with no subject is yours.
    clipped = S.extract_commitments([
        {"body": "Looking at it now, will confirm by Wednesday."},
        {"body": "Sending the deck tonight."},
    ], now=MON)
    check("a clipped 'will confirm' counts",
          any("will confirm" in t for t in texts(clipped)), str(texts(clipped)))
    check("so does 'Sending the deck'",
          any("sending the deck" in t for t in texts(clipped)), str(texts(clipped)))


def test_what_is_not_a_promise() -> None:
    print("requests and other people's promises are not yours")
    found = S.extract_commitments([
        {"body": "Can you send the Q4 numbers by Wednesday?"},
        {"body": "Could you review the SOW tonight?"},
        {"body": "He will send the pack on Friday."},
        {"body": "Scott will confirm the numbers."},
        {"body": "She said she'll handle the renewal."},
        {"body": "Please confirm receipt."},
        {"body": "If you want, I'll send it over."},
        {"body": "Would you like me to send the deck?"},
        {"body": ""},
    ], now=MON)
    check("nothing was extracted", found == [], str(texts(found)))

    # The hedge matters: "if you want, I'll send it" is an offer, not a promise.
    check("a hedged offer is excluded",
          not S.extract_commitments([{"body": "Unless you object, I'll send it."}], now=MON))


def test_promise_dates_and_dedupe() -> None:
    print("a promise keeps its date and its exact words")
    found = S.extract_commitments(
        [{"body": "I'll send the forecast by Wednesday.", "to": "scott@x.com",
          "item_id": "m1"}], now=MON)
    c = found[0]
    check("a weekday in the sentence becomes a due date", bool(c.due_at), c.due_at)
    check("and it is this Wednesday",
          datetime.fromisoformat(c.due_at).date() == (MON + timedelta(days=2)).date(),
          c.due_at)
    check("the recipient is recorded", c.owed_to == "scott@x.com")
    check("the item is recorded", c.item_id == "m1")
    check("the original sentence is quoted", "forecast" in c.source_quote)

    undated = S.extract_commitments([{"body": "I'll get to it soon."}], now=MON)
    check("no date stated means no date invented", undated[0].due_at == "")

    twice = S.extract_commitments([
        {"body": "I'll send the forecast by Wednesday."},
        {"body": "As promised, I'll send the forecast by Wednesday."},
    ], now=MON)
    check("the same promise twice is one commitment", len(twice) == 1, str(texts(twice)))


def test_commitments_survive_being_closed() -> None:
    print("closing a commitment sticks across re-extraction")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        rows = [c.to_dict() for c in S.extract_commitments(
            [{"body": "I'll send the forecast by Wednesday."},
             {"body": "I'll book the review."}], now=MON)]
        check("two stored", store.replace_commitments(rows) == 2)

        first = store.commitments()[0]
        store.set_commitment_state(first["id"], "done")
        check("one is closed",
              [c["state"] for c in store.commitments()].count("done") == 1)

        # Re-running extraction over the same messages must not reopen it.
        store.replace_commitments(rows)
        states = {c["text"]: c["state"] for c in store.commitments()}
        check("the closed one is not resurrected", first["text"] not in
              [c["text"] for c in store.commitments() if c["state"] == "open"],
              str(states))
        check("the open one is still open", "open" in states.values())
        check("re-running does not duplicate", len(store.commitments()) == 2,
              str(len(store.commitments())))


def test_extraction_never_reads_anyone_else() -> None:
    print("extraction only ever sees what you wrote")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        store.upsert_item(Item(id="m1", source="mail", kind="email", subject="SOW"))
        store.set_thread("m1", [
            {"author": "Priya Raman", "body": "I'll send you our security pack Friday.",
             "is_me": False},
            {"author": "You", "body": "I'll review it Monday.", "is_me": True},
        ])
        mine = store.my_messages()
        check("only your message is returned", len(mine) == 1, str(mine))
        check("and it is yours", "review it Monday" in mine[0]["body"])

        found = S.extract_commitments(mine)
        check("their promise is not yours",
              not any("security pack" in t for t in texts(found)), str(texts(found)))
        check("yours is", any("i'll review" in t for t in texts(found)))


# ── summaries ───────────────────────────────────────────────────────────────

def rows_for(now: datetime) -> list[dict]:
    def row(**kw):
        base = {"id": "x", "source": "mail", "kind": "email", "subject": "s",
                "state": "needs_me", "starts_at": "", "ends_at": "", "received_at": "",
                "replied_at": "", "attendees": [], "has_agenda": True,
                "rsvp_pending": False, "conflicts_with": [], "external_attendees": False,
                "sent_by_me_at": "", "snippet": ""}
        base.update(kw)
        flag = base.pop("flag", {"quadrant": "neither", "pinned": False})
        return {"item": base, "flag": flag}

    return [
        row(id="cal1", source="calendar", kind="event", subject="1:1",
            starts_at=iso(now.replace(hour=10)), ends_at=iso(now.replace(hour=11)),
            has_agenda=False, attendees=["Scott"]),
        row(id="cal2", source="calendar", kind="event", subject="Deep dive",
            starts_at=iso(now.replace(hour=14)), ends_at=iso(now.replace(hour=15)),
            rsvp_pending=True, external_attendees=True),
        row(id="cal3", source="calendar", kind="event", subject="Tomorrow standup",
            starts_at=iso((now + timedelta(days=1)).replace(hour=9, minute=30)),
            ends_at=iso((now + timedelta(days=1)).replace(hour=10))),
        row(id="m1", subject="Forecast", received_at=iso(now - timedelta(hours=3)),
            flag={"quadrant": "important_urgent", "pinned": False}),
        row(id="m2", subject="Territory planning", received_at=iso(now - timedelta(days=1)),
            flag={"quadrant": "important_only", "pinned": False}),
        row(id="m3", subject="Cleared thing", state="done",
            received_at=iso(now - timedelta(hours=8)),
            replied_at=iso(now - timedelta(hours=6)),
            flag={"quadrant": "neither", "pinned": True}),
        row(id="m4", subject="SOW redlines", state="waiting",
            sent_by_me_at=iso(now - timedelta(days=5)),
            flag={"quadrant": "neither", "pinned": False}),
    ]


def test_morning_brief() -> None:
    print("the morning brief")
    b = S.morning_brief(rows_for(MON), now=MON)
    check("today's meetings only", [m["id"] for m in b["meetings"]] == ["cal1", "cal2"],
          str([m["id"] for m in b["meetings"]]))
    check("in time order", b["meetings"][0]["starts_at"] < b["meetings"][1]["starts_at"])
    check("a missing agenda is named", "no agenda posted" in b["meetings"][0]["gaps"])
    check("a pending RSVP is named", "RSVP still pending" in b["meetings"][1]["gaps"])
    check("external attendees are named",
          "external attendees" in b["meetings"][1]["gaps"])
    check("a meeting with no gaps says nothing", b["meetings"][0]["gaps"] ==
          ["no agenda posted"], str(b["meetings"][0]["gaps"]))

    check("needs-reply holds the urgent ones", b["needs_reply"] == ["m1"],
          str(b["needs_reply"]))
    check("important-but-not-urgent gets the time block",
          b["time_block"]["items"] == ["m2"], str(b["time_block"]["items"]))
    check("waiting-on is listed", b["waiting_on"] == ["m4"], str(b["waiting_on"]))


def test_time_block_comes_from_the_calendar() -> None:
    print("the suggested time block is computed, not guessed")
    tomorrow = (MON + timedelta(days=1)).replace(hour=0, minute=0)

    free = S.suggest_block([], now=MON)
    check("an empty calendar offers 09:00",
          free["found"] and hhmm(free["start"]) == "09:00", str(free))

    blocked = [{"starts_at": iso(tomorrow.replace(hour=h)),
                "ends_at": iso(tomorrow.replace(hour=h + 1))} for h in range(9, 17)]
    full = S.suggest_block(blocked, now=MON)
    check("a full day says so, rather than inventing a slot", full["found"] is False)
    check("and explains why", "booked solid" in full["reason"], str(full))

    gap = [{"starts_at": iso(tomorrow.replace(hour=9)),
            "ends_at": iso(tomorrow.replace(hour=10))}]
    after = S.suggest_block(gap, now=MON)
    check("a morning meeting pushes the block after it",
          after["found"] and hhmm(after["start"]) == "10:00", str(after))

    # Only tomorrow's meetings matter; today's are already behind you.
    today_only = [{"starts_at": iso(MON.replace(hour=9)),
                   "ends_at": iso(MON.replace(hour=17))}]
    check("today's calendar does not block tomorrow",
          S.suggest_block(today_only, now=MON)["found"] is True)


def test_end_of_day() -> None:
    print("the end-of-day recap")
    r = S.end_of_day(rows_for(MON), now=MON)
    check("handled lists what you cleared", r["handled"] == ["m3"], str(r["handled"]))
    check("still-open excludes meetings",
          set(r["still_open"]) == {"m1", "m2"}, str(r["still_open"]))
    check("tomorrow's meetings only", [m["id"] for m in r["tomorrow"]] == ["cal3"],
          str(r["tomorrow"]))
    check("capped at three", len(r["tomorrow"]) <= 3)


def test_weekly_metrics_are_counted_not_guessed() -> None:
    print("weekly metrics")
    rows = rows_for(MON)
    log = [{"at": iso(MON - timedelta(hours=2))}, {"at": iso(MON - timedelta(days=30))}]
    w = S.weekly_metrics(rows, log, now=MON)

    check("cleared items are counted", w["items_cleared"] == 1, str(w["items_cleared"]))
    check("response time is measured from the row", w["median_response_hours"] == 2.0,
          str(w["median_response_hours"]))
    check("and says how many replies it saw", w["replies_counted"] == 1)
    check("meetings this week are counted", w["meetings_total"] == 3, str(w))
    check("per-day divides by days that had meetings", w["meetings_per_day"] == 1.5,
          str(w["meetings_per_day"]))
    check("flag accuracy counts corrections",
          w["flags_corrected"] == 1 and w["flags_total"] == 7, str(w))
    check("and reports a percentage", w["flag_accuracy"] == 85.7, str(w["flag_accuracy"]))
    check("old log lines are outside the week", w["actions_logged"] == 1,
          str(w["actions_logged"]))

    empty = S.weekly_metrics([], [], now=MON)
    check("no data shows a dash, not a zero that looks like a measurement",
          empty["median_response_hours"] is None and empty["flag_accuracy"] is None,
          str(empty))


# ── scheduling ──────────────────────────────────────────────────────────────

def test_due_now() -> None:
    print("a summary runs once a day, even if the machine was asleep")
    times = {"brief": "08:00", "recap": "17:30"}
    today = MON.date().isoformat()

    morning = S.due_now(MON.replace(hour=8, minute=1), times, {})
    check("the brief is due just after 08:00", morning.brief)
    check("the recap is not", not morning.recap)
    check("Monday also owes a weekly recap", morning.weekly)

    check("nothing is due before the time",
          not S.due_now(MON.replace(hour=7, minute=59), times, {}).brief)

    done = {"brief": today, "weekly": today}
    check("a brief already generated today does not run again",
          not S.due_now(MON.replace(hour=11), times, done).brief)
    check("nor the weekly", not S.due_now(MON.replace(hour=11), times, done).weekly)

    evening = S.due_now(MON.replace(hour=18), times, done)
    check("the recap is due after 17:30", evening.recap)
    check("and names what it is for", "end-of-day recap" in evening.reasons[0],
          str(evening.reasons))

    # Asleep at 08:00, woken at 14:00: the brief is still owed.
    late = S.due_now(MON.replace(hour=14), times, {})
    check("a machine asleep at 08:00 still gets its brief", late.brief)

    tuesday = (MON + timedelta(days=1)).replace(hour=9)
    check("Tuesday owes no weekly recap",
          not S.due_now(tuesday, times, {}).weekly)
    check("but still owes a brief", S.due_now(tuesday, times, {}).brief)

    check("a bad time string falls back rather than crashing",
          S.due_now(MON.replace(hour=12), {"brief": "oops"}, {}).brief)


# ── through the bridge ──────────────────────────────────────────────────────

def test_summaries_through_the_api() -> None:
    print("summaries through the bridge")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "helm.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        api = Api(store, EventBus(), demo=True)
        api.sync()

        s = api.summaries()
        for key in ("brief", "recap", "weekly", "commitments", "last_run"):
            check(f"summaries carry {key}", key in s)
        check("the brief found demo meetings", bool(s["brief"]["meetings"]), str(s["brief"]))

        res = api.refresh_commitments()
        check("extraction ran", "found" in res and "commitments" in res)
        check("demo fixtures contain a promise you made", res["open"] > 0, str(res))

        first = res["commitments"][0]
        closed = api.set_commitment_state(first["id"], "done")
        check("a commitment can be closed",
              any(c["state"] == "done" for c in closed["commitments"]))
        check("closing is logged",
              store.action_log()[0]["verb"] == "commitment")

        boot = api.bootstrap()
        check("bootstrap carries the summaries", "summaries" in boot)
        check("so the Recap tab needs no second round-trip",
              "weekly" in boot["summaries"])


def test_scheduling_records_what_it_ran() -> None:
    print("the local timer generates each summary once")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "helm.db")
        for key, value in DEMO_SETTINGS.items():
            store.set_setting(key, value)
        # Both times in the past, so everything is due on the first tick.
        store.set_setting("summary_times", {"brief": "00:01", "recap": "00:02"})
        seen: list[dict] = []
        bus = EventBus()
        bus.subscribe("summary_ready", seen.append)
        api = Api(store, bus, demo=True)
        api.sync()

        api._run_due_summaries()
        check("the UI was told once", len(seen) == 1, str(seen))
        check("and told what was generated", bool(seen[0]["due"]))
        check("the run was recorded", bool(store.get_setting("summary_last_run")))
        check("it is logged", any(a["verb"] == "summary" for a in store.action_log()))

        api._run_due_summaries()
        check("a second tick the same day generates nothing", len(seen) == 1,
              str(len(seen)))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
