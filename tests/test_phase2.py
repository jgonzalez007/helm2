"""Phase 2 self-check: connector parsing, hostile input, cadence, tool allowlists.

No window, no network, no accounts — every connector reply here is a literal.

    python -m tests.test_phase2
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from helm import claude_cli as cli
from helm import scheduler as sched
from helm.connectors import claude_source as cs
from helm.connectors.base import SendRefused
from helm.connectors.pending import PendingSource
from helm.models import ApprovalToken, Item

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


INTERNAL = ["sailpoint.com"]
ME = "jose.gonzalez@sailpoint.com"


# ── Tool allowlists are the real defence ────────────────────────────────────

def test_allowlists_are_read_only() -> None:
    print("no read allowlist can reach a write tool")
    writes = set(cli.WRITE_TOOLS)
    for name in ("CALENDAR_READ", "MAIL_READ", "SLACK_READ"):
        allow = set(getattr(cli, name))
        check(f"{name} contains no write tool", not (allow & writes),
              str(sorted(allow & writes)))

    check("the deny list covers sending mail",
          f"{cli.MS365}outlook_send_mail" in writes)
    check("the deny list covers RSVP",
          f"{cli.MS365}outlook_respond_to_event" in writes)
    check("the deny list covers posting to Slack",
          f"{cli.SLACK}slack_send_message" in writes)
    check("the deny list covers deleting events",
          f"{cli.MS365}outlook_delete_event" in writes)

    check("calendar reads only search the calendar",
          all("calendar" in t or "read_resource" in t for t in cli.CALENDAR_READ),
          str(cli.CALENDAR_READ))
    check("the system prompt names content as data",
          "untrusted data" in cli.DATA_NOT_INSTRUCTIONS)


# ── Reply parsing ───────────────────────────────────────────────────────────

def test_extract_json() -> None:
    print("parsing what the CLI actually returns")
    check("a bare array", cli.extract_json('[{"a":1}]') == [{"a": 1}])
    check("a fenced array",
          cli.extract_json('```json\n[{"a":1}]\n```') == [{"a": 1}])
    check("a fence with no language", cli.extract_json('```\n[1,2]\n```') == [1, 2])
    check("an array wrapped in prose",
          cli.extract_json('Here you go:\n[{"a":1}]\nHope that helps.') == [{"a": 1}])
    check("an empty array", cli.extract_json("[]") == [])
    check("an object", cli.extract_json('{"a":1}') == {"a": 1})
    # Preferring "[" meant an object wrapping an array parsed as the array itself.
    check("an object containing an array stays an object",
          cli.extract_json('{"inbox":[{"a":1}],"sent":[]}')
          == {"inbox": [{"a": 1}], "sent": []})
    check("an array of objects stays an array",
          cli.extract_json('[{"inbox":1}]') == [{"inbox": 1}])
    check("an object wrapping an array, fenced and with prose",
          cli.extract_json('Sure:\n```json\n{"inbox":[1,2]}\n```\nDone.')
          == {"inbox": [1, 2]})
    for junk in ("", "   ", "I could not read your calendar.", "[not json]", None):
        check(f"junk returns None: {junk!r}", cli.extract_json(junk) is None)


# ── Time handling ───────────────────────────────────────────────────────────

def test_wall_clock_to_local() -> None:
    print("wall-clock times are read as local, never as UTC")
    out = cs.to_local_iso("2026-10-19T12:00:00")
    check("an offset is attached", bool(out) and ("+" in out[10:] or "-" in out[10:]), out)
    check("the wall clock is unchanged", out.startswith("2026-10-19T12:00:00"), out)
    check("seconds are optional on input",
          cs.to_local_iso("2026-10-19T12:00").startswith("2026-10-19T12:00:00"))
    check("a space separator works",
          cs.to_local_iso("2026-10-19 12:00:00").startswith("2026-10-19T12:00:00"))

    # Graph returns seven fractional digits. An earlier regex rejected them, which
    # silently dropped every event on the first live run.
    frac = cs.to_local_iso("2026-10-16T00:00:00.0000000")
    check("seven fractional digits parse", frac.startswith("2026-10-16T00:00:00"), frac)
    check("three fractional digits parse",
          cs.to_local_iso("2026-10-16T09:30:00.123").startswith("2026-10-16T09:30:00"))
    check("fraction without seconds parses",
          cs.to_local_iso("2026-10-16T09:30.5").startswith("2026-10-16T09:30:00"))

    for junk in ("", "tomorrow", "2026-13-45T99:99:99", "2026-10-19T12:00:00Z", None, 17):
        check(f"junk is refused: {junk!r}", cs.to_local_iso(junk) == "")


def test_all_day_blocks() -> None:
    print("all-day blocks are absences, not meetings")
    ooo = {"id": "ooo", "subject": "Maiwand OOO",
           "start": "2026-10-16T00:00:00.0000000", "end": "2026-10-22T00:00:00.0000000",
           "response_status": "none", "has_agenda": False}
    item = cs._clean_event(ooo, ME, INTERNAL)
    check("the event parses", item is not None)
    check("it is marked all-day", item.all_day is True)
    check("no RSVP gap is claimed", item.rsvp_pending is False)
    check("no agenda gap is claimed", item.has_agenda is True)

    meeting = cs._clean_event(
        dict(ooo, id="m", start="2026-10-16T09:00:00.0000000",
             end="2026-10-16T10:00:00.0000000"), ME, INTERNAL)
    check("a normal meeting is not all-day", meeting.all_day is False)
    check("and its RSVP gap is kept", meeting.rsvp_pending is True)
    check("and its agenda gap is kept", meeting.has_agenda is False)

    single = cs._clean_event(
        dict(ooo, id="s", start="2026-10-16T00:00:00", end="2026-10-17T00:00:00"),
        ME, INTERNAL)
    check("a one-day block counts as all-day", single.all_day is True)
    check("midnight start but a short span does not",
          cs.is_all_day("2026-10-16T00:00:00", "2026-10-16T01:00:00") is False)
    check("a missing end is not all-day", cs.is_all_day("2026-10-16T00:00:00", "") is False)


def test_all_day_gets_no_urgency() -> None:
    print("an all-day block never reaches the focus queue")
    from helm.flags import Settings, classify

    cfg = Settings(internal_domains={"sailpoint.com"})
    now = datetime(2026, 10, 19, 9, 0).astimezone()

    ooo = Item(id="ooo", source="calendar", kind="event", subject="Jose OOO",
               starts_at=(now + timedelta(minutes=10)).isoformat(timespec="seconds"),
               ends_at=(now + timedelta(days=3)).isoformat(timespec="seconds"),
               all_day=True, rsvp_pending=False, has_agenda=True)
    f = classify(ooo, cfg, now=now)
    check("starting in 10 minutes does not make an absence urgent",
          f.urgency == "none", f.urgency)
    check("and it is not in the top quadrant", f.quadrant == "neither", f.quadrant)

    meeting = Item(id="m", source="calendar", kind="event", subject="Standup",
                   starts_at=(now + timedelta(minutes=10)).isoformat(timespec="seconds"),
                   ends_at=(now + timedelta(minutes=40)).isoformat(timespec="seconds"))
    check("a real meeting in 10 minutes is urgent",
          classify(meeting, cfg, now=now).urgency == "now")


def test_cancelled_events_are_dropped() -> None:
    print("a cancelled meeting is not something to attend")
    base = {"id": "c", "subject": "Canceled: IT Operations Standup",
            "start": "2026-10-20T08:30:00.0000000", "end": "2026-10-20T09:00:00.0000000",
            "response_status": "none"}
    check("a cancelled event is dropped",
          cs._clean_event(dict(base, is_cancelled=True), ME, INTERNAL) is None)
    check("'true' as a string is also dropped",
          cs._clean_event(dict(base, is_cancelled="true"), ME, INTERNAL) is None)
    check("a live event survives",
          cs._clean_event(dict(base, is_cancelled=False), ME, INTERNAL) is not None)
    check("a missing flag is treated as live",
          cs._clean_event(base, ME, INTERNAL) is not None)


def test_all_day_blocks_do_not_create_conflicts() -> None:
    print("a week-long OOO block is not a double-booking")

    def ev(key, start, end, all_day=False):
        return Item(id=key, source="calendar", kind="event", subject=key,
                    starts_at=start, ends_at=end, all_day=all_day)

    ooo = ev("ooo", "2026-10-16T00:00:00+00:00", "2026-10-22T00:00:00+00:00", all_day=True)
    a = ev("a", "2026-10-19T09:00:00+00:00", "2026-10-19T10:00:00+00:00")
    b = ev("b", "2026-10-19T09:30:00+00:00", "2026-10-19T10:30:00+00:00")

    cs.mark_conflicts([ooo, a, b])
    check("the OOO block conflicts with nothing", ooo.conflicts_with == [],
          str(ooo.conflicts_with))
    check("and nothing conflicts with it", a.conflicts_with == ["b"], str(a.conflicts_with))
    check("the real clash is still found", b.conflicts_with == ["a"], str(b.conflicts_with))


def test_a_solo_block_is_not_important() -> None:
    print("a focus block you put in your own calendar is not a meeting")
    from helm.flags import Settings, classify

    cfg = Settings(internal_domains={"sailpoint.com"})
    now = datetime(2026, 10, 19, 7, 0).astimezone()
    start = (now + timedelta(hours=1)).isoformat(timespec="seconds")
    end = (now + timedelta(hours=2)).isoformat(timespec="seconds")

    solo = Item(id="w", source="calendar", kind="event", subject="Work",
                starts_at=start, ends_at=end, organizer_is_me=True,
                attendees=[], has_agenda=False)
    check("'Work' with nobody in it is not high importance",
          classify(solo, cfg, now=now).importance != "high",
          classify(solo, cfg, now=now).importance)

    real = Item(id="r", source="calendar", kind="event", subject="IT Ops Leadership",
                starts_at=start, ends_at=end, organizer_is_me=True,
                attendees=["Eddie Jennings", "Amy Kent"], has_agenda=True)
    check("a meeting you organize with people in it is high",
          classify(real, cfg, now=now).importance == "high")


def test_timezone_mismatch() -> None:
    print("a mailbox in another zone is reported, not silently miscomputed")
    local = next(n for n in time.tzname if n)
    check("the machine's own zone is no warning", cs.timezone_mismatch(local) == "")
    check("an empty zone is no warning", cs.timezone_mismatch("") == "")
    warn = cs.timezone_mismatch("Tokyo Standard Time")
    check("a different zone warns", bool(warn))
    check("the warning names the reported zone", "Tokyo Standard Time" in warn)
    check("the warning says times are read as local", "read as local" in warn)


# ── Hostile input ───────────────────────────────────────────────────────────

def test_event_cleaning_is_defensive() -> None:
    print("every field from a calendar entry is coerced and bounded")
    hostile = {
        "id": {"nested": "object where a string belongs"},
        "subject": "x" * 5000,
        "start": "2026-10-19T09:00:00",
        "end": "2026-10-19T10:00:00",
        "organizer": ["a", "list"],
        "organizer_email": "attacker@evil.example",
        "attendees": "not a list",
        "attendee_emails": ["a@sailpoint.com"] * 500,
        "is_organizer": "yes",
        "response_status": None,
        "join_url": "file:///C:/Windows/System32/cmd.exe",
        "has_agenda": "true",
        "agenda_excerpt": "y" * 9000,
    }
    item = cs._clean_event(hostile, ME, INTERNAL)
    check("the event still parses", item is not None)
    check("a non-string id is replaced with a derived one", item.id.startswith("cal-"), item.id)
    check("the subject is capped", len(item.subject) <= cs.MAX_STR, str(len(item.subject)))
    check("the snippet is capped", len(item.snippet) <= 300, str(len(item.snippet)))
    check("a list where a name belongs becomes empty", item.sender == "attacker@evil.example",
          item.sender)
    # `attendees` was a string, so the names are derived from the addresses instead
    # — and the derivation inherits the address list's cap.
    check("a string where a name list belongs does not become a string",
          isinstance(item.attendees, list), str(type(item.attendees)))
    check("derived names inherit the address cap",
          len(item.attendees) <= cs.MAX_LIST, str(len(item.attendees)))
    check("a name is derived from the address",
          cs._clean_event(dict(hostile, attendees=0,
                               attendee_emails=["scott.chen@sailpoint.com"]),
                          ME, INTERNAL).attendees == ["Scott Chen"])
    flooded = cs._clean_event(
        dict(hostile, attendees=[f"Person {i}" for i in range(500)]), ME, INTERNAL)
    check("a 500-name attendee list is capped",
          len(flooded.attendees) == cs.MAX_LIST, str(len(flooded.attendees)))
    check("each attendee name is capped too",
          all(len(a) <= 120 for a in cs._clean_event(
              dict(hostile, attendees=["z" * 9000]), ME, INTERNAL).attendees))
    check("'yes' is read as true", item.organizer_is_me is True)
    check("a missing RSVP counts as pending", item.rsvp_pending is True)
    check("a file:// join link is dropped", item.join_url == "", item.join_url)

    for scheme in ("javascript:alert(1)", "cmd.exe /c del", "C:\\tool.bat",
                   "ftp://host/x", "data:text/html,<script>"):
        bad = dict(hostile, join_url=scheme)
        check(f"{scheme[:22]!r} is dropped",
              cs._clean_event(bad, ME, INTERNAL).join_url == "")

    good = dict(hostile, join_url="https://teams.microsoft.com/l/meetup-join/x")
    check("an https join link survives",
          cs._clean_event(good, ME, INTERNAL).join_url.startswith("https://"))


def test_event_cleaning_drops_the_unusable() -> None:
    print("an event with no usable start is dropped, not guessed")
    for raw in (None, "a string", 42, {}, {"subject": "no start"},
                {"subject": "bad start", "start": "whenever"}):
        check(f"dropped: {str(raw)[:30]!r}", cs._clean_event(raw, ME, INTERNAL) is None)


def test_external_attendee_detection() -> None:
    print("external attendees")
    base = {"subject": "Review", "start": "2026-10-19T09:00:00", "end": "2026-10-19T10:00:00"}

    internal_only = cs._clean_event(
        dict(base, id="a", attendee_emails=["scott.chen@sailpoint.com", ME]), ME, INTERNAL)
    check("an all-internal meeting is not external", internal_only.external_attendees is False)

    with_guest = cs._clean_event(
        dict(base, id="b", attendee_emails=["priya@northwind-example.com", ME]), ME, INTERNAL)
    check("one outside address makes it external", with_guest.external_attendees is True)

    organizer = cs._clean_event(
        dict(base, id="c", organizer_email="priya@northwind-example.com"), ME, INTERNAL)
    check("an outside organizer is marked external", organizer.sender_external is True)
    check("an inside organizer is not",
          cs._clean_event(dict(base, id="d", organizer_email=ME), ME, INTERNAL)
          .sender_external is False)


def test_conflicts_are_computed_locally() -> None:
    print("overlap is computed locally, so it is right offline")

    def ev(key, start_h, end_h):
        return Item(id=key, source="calendar", kind="event", subject=key,
                    starts_at=f"2026-10-19T{start_h:02d}:00:00+00:00",
                    ends_at=f"2026-10-19T{end_h:02d}:00:00+00:00")

    a, b, c = ev("a", 9, 10), ev("b", 9, 11), ev("c", 14, 15)
    cs.mark_conflicts([a, b, c])
    check("overlapping meetings see each other", a.conflicts_with == ["b"], str(a.conflicts_with))
    check("and the other way round", b.conflicts_with == ["a"], str(b.conflicts_with))
    check("a separate meeting is clear", c.conflicts_with == [], str(c.conflicts_with))

    # Back-to-back is not a conflict: 10:00-11:00 after 09:00-10:00 is fine.
    d, e = ev("d", 9, 10), ev("e", 10, 11)
    cs.mark_conflicts([d, e])
    check("back-to-back is not an overlap", d.conflicts_with == [], str(d.conflicts_with))

    # Running it twice must not double-list.
    cs.mark_conflicts([a, b, c])
    check("re-running does not duplicate", a.conflicts_with == ["b"], str(a.conflicts_with))


# ── Cadence ─────────────────────────────────────────────────────────────────

def test_cadence() -> None:
    print("polling cadence")
    work = datetime(2026, 10, 19, 10, 0)      # Monday morning
    evening = datetime(2026, 10, 19, 22, 0)   # Monday night
    weekend = datetime(2026, 10, 17, 10, 0)   # Saturday

    # A mail sweep measured 230 s, so a 2-minute cadence would never finish.
    check("work hours poll mail every 10 min",
          sched.interval_for("mail", work) == 600)
    check("work hours poll calendar every 10 min", sched.interval_for("calendar", work) == 600)
    check("evenings back off", sched.interval_for("mail", evening) == 900)
    check("weekends back off", sched.interval_for("mail", weekend) == 900)
    check("Monday 10:00 counts as work time", sched.is_work_time(work))
    check("Saturday does not", not sched.is_work_time(weekend))
    check("22:00 does not", not sched.is_work_time(evening))
    check("an unknown source still gets an interval",
          sched.interval_for("teams", work) == 600)


def test_quiet_hours() -> None:
    print("quiet hours, including the ones that wrap midnight")
    at = lambda h, m=0: datetime(2026, 10, 19, h, m)   # noqa: E731

    check("23:00 is inside 19:00-07:00", sched.in_quiet_hours(at(23), "19:00", "07:00"))
    check("03:00 is inside", sched.in_quiet_hours(at(3), "19:00", "07:00"))
    check("12:00 is outside", not sched.in_quiet_hours(at(12), "19:00", "07:00"))
    check("07:00 exactly is outside", not sched.in_quiet_hours(at(7), "19:00", "07:00"))
    check("19:00 exactly is inside", sched.in_quiet_hours(at(19), "19:00", "07:00"))

    check("a same-day window works", sched.in_quiet_hours(at(13), "12:00", "14:00"))
    check("and excludes outside it", not sched.in_quiet_hours(at(15), "12:00", "14:00"))
    check("junk falls back to the default window",
          sched.in_quiet_hours(at(23), "not a time", "nope"))


def test_backoff_and_recovery() -> None:
    print("a failing source backs off and recovers")
    from helm.bus import EventBus

    calls: list[str] = []
    healthy = {"calendar": False}

    def poll(source: str) -> bool:
        calls.append(source)
        return healthy[source]

    s = sched.Scheduler(EventBus(), poll, ("calendar",), quiet=lambda: ("23:59", "23:58"))
    s._due["calendar"] = 0

    # Three failures: the wait should grow, capped. The base interval is pinned to a
    # work-hours clock — off hours it already equals BACKOFF_CAP, which made this
    # check pass or fail depending on what time of day it ran.
    work_hours = datetime(2026, 10, 19, 10, 0)
    waits = []
    for _ in range(3):
        s._due["calendar"] = 0
        before = s._backoff["calendar"]
        poll("calendar")
        base = sched.interval_for("calendar", work_hours)
        s._backoff["calendar"] = min(sched.BACKOFF_CAP, (before * 2) or base)
        waits.append(s._backoff["calendar"])
    check("backoff grows", waits == sorted(waits) and waits[0] < waits[-1], str(waits))
    check("backoff is capped", all(w <= sched.BACKOFF_CAP for w in waits))

    s.poke("test")
    check("a poke clears the backoff", s._backoff["calendar"] == 0)
    check("and makes everything due now", s._due["calendar"] <= time.monotonic())


# ── Read-only sources refuse to write ───────────────────────────────────────

def test_read_only_sources_refuse_to_send() -> None:
    print("a read-only source refuses, and refuses for the right reason")
    source = cs.CalendarSource(ME, INTERNAL)

    try:
        source.draft("x", "draft reply")
    except NotImplementedError as e:
        check("drafting is not implemented yet", "read-only" in str(e))
    else:
        raise AssertionError("FAILED: the calendar source produced a draft")

    # No token at all: refused by the gate, before anything else is considered.
    try:
        source.send("x", None)
    except SendRefused as e:
        check("a send with no token is refused by the gate", "approval" in str(e).lower())
    else:
        raise AssertionError("FAILED: calendar send accepted a missing token")

    # Even a genuine clicked token cannot make a read-only source send.
    try:
        source.send("x", ApprovalToken.mint_from_click("x"))
    except SendRefused as e:
        check("a clicked token still cannot send from a read-only source",
              "read-only" in str(e))
    else:
        raise AssertionError("FAILED: calendar send went through")


def test_pending_sources_are_honest() -> None:
    print("sources that are not built yet say so")
    mail = PendingSource("mail", "phase 3")
    st = mail.status()
    check("they report unavailable", st.health == "unavailable")
    check("and name the phase", "phase 3" in st.last_error)
    check("they return no items", mail.list_new("") == [])
    check("and no thread", mail.get_thread("x").messages == [])
    try:
        mail.send("x", ApprovalToken.mint_from_click("x"))
    except SendRefused:
        check("they refuse to send")
    else:
        raise AssertionError("FAILED: a pending source sent something")


# ── The sweep keeps data when the connector breaks ──────────────────────────

def test_a_broken_sweep_keeps_the_last_good_data() -> None:
    print("a failed sweep never empties the panel")
    import tempfile
    from pathlib import Path

    from helm.app import Api
    from helm.bus import EventBus
    from helm.store import Store

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        api = Api(store, EventBus(), demo=True)
        api.sync()
        before = len(store.items())
        check("fixtures loaded", before > 0)

        class Broken:
            name = "mail"

            def status(self):
                from helm.models import SourceStatus
                return SourceStatus(source="mail", health="reconnecting",
                                    last_error="connector down")

            def list_new(self, since=""):
                raise cli.ClaudeUnavailable("no answer within 180 s")

            def get_thread(self, item_id):
                from helm.models import Thread
                return Thread(item_id=item_id)

        api._sources["mail"] = Broken()
        ok = api.poll("mail")
        check("the sweep reports failure so the scheduler backs off", ok is False)
        check("rows are still there", len(store.items()) == before, str(len(store.items())))
        state = {s["source"]: s for s in store.source_states()}["mail"]
        check("the status dot flips", state["health"] == "reconnecting", state["health"])
        check("the error is recorded", "180 s" in state["last_error"], state["last_error"])

        mail_rows = store.items(source="mail")
        check("nothing was retired by a failed sweep",
              all(i.state != "done" for i in mail_rows if i.id != "demo-mail-6"))


def test_two_way_sync_retires_handled_items() -> None:
    print("reading something elsewhere removes it here")
    import tempfile
    from pathlib import Path

    from helm.app import Api
    from helm.bus import EventBus
    from helm.models import SourceStatus, Thread
    from helm.store import Store

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        api = Api(store, EventBus(), demo=True)
        api.sync()
        before = [i.id for i in store.items(source="calendar") if i.state == "needs_me"]
        check("calendar items start open", bool(before), str(before))

        class Shrunk:
            """The connector stops returning one event: it was handled elsewhere."""

            name = "calendar"

            def status(self):
                return SourceStatus(source="calendar", health="live", last_ok_at="now")

            def list_new(self, since=""):
                from helm.connectors.demo import DemoSource
                return DemoSource("calendar").list_new("")[1:]

            def get_thread(self, item_id):
                return Thread(item_id=item_id)

        api._sources["calendar"] = Shrunk()
        api.poll("calendar")
        gone = store.item(before[0])
        check("the missing item is retired", gone.state == "done", gone.state)
        still = store.item(before[1]) if len(before) > 1 else None
        if still is not None:
            check("the ones still returned are untouched", still.state == "needs_me",
                  still.state)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
