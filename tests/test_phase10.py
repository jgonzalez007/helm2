"""Phase 10: sending, and everything standing in front of it.

No window, no network, no real send: `cli.run_write` is replaced with a recorder, so
these checks can assert exactly which tool would have been called, with what, and
how often — which is the only way to test a send path without sending anything.

    python -m tests.test_phase10
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from helm import app as A
from helm import claude_cli as cli
from helm.bus import EventBus
from helm.connectors.base import SendRefused
from helm.connectors.claude_source import CalendarSource
from helm.connectors.demo import DEMO_SETTINGS
from helm.connectors.mail_source import MailSource
from helm.connectors.slack_source import SlackSource
from helm.models import ApprovalToken, Draft, Item
from helm.store import Store

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


class Recorder:
    """Stands in for cli.run_write and records what a real send would have done."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, tool, prompt, approval, timeout=cli.WRITE_TIMEOUT):
        self.calls.append({"tool": tool, "prompt": prompt, "approval": approval})
        if self.fail:
            raise self.fail
        return "SENT"


def with_recorder(fail: Exception | None = None) -> Recorder:
    rec = Recorder(fail)
    cli.run_write = rec           # restored by restore()
    return rec


_REAL_RUN_WRITE = cli.run_write


def restore() -> None:
    cli.run_write = _REAL_RUN_WRITE


def a_draft(channel="mail") -> Draft:
    return Draft(id="d1", item_id="m1", channel=channel,
                 to_addr="scott.chen@sailpoint.com" if channel == "mail" else "#deal-acme",
                 subject="Re: Q4 forecast",
                 body="Scott — numbers land Wednesday morning.")


# ── the gate itself ─────────────────────────────────────────────────────────

def test_run_write_needs_a_clicked_token() -> None:
    print("run_write cannot be called without a click")
    restore()
    tool = f"{cli.MS365}outlook_send_mail"

    for bogus in (None, "token", 42, {"draft_id": "d1"}, object()):
        try:
            cli.run_write(tool, "prompt", bogus)
        except PermissionError as e:
            check(f"{type(bogus).__name__} is refused", "clicked" in str(e).lower())
        else:
            raise AssertionError(f"FAILED: run_write accepted {bogus!r}")

    token = ApprovalToken.mint_from_click("d1")
    try:
        cli.run_write("mcp__something__else", "prompt", token)
    except PermissionError as e:
        check("an unknown tool is refused even with a real token",
              "not a known write tool" in str(e))
    else:
        raise AssertionError("FAILED: run_write accepted an arbitrary tool name")

    for read_tool in cli.MAIL_READ + cli.SLACK_READ + cli.CALENDAR_READ:
        try:
            cli.run_write(read_tool, "prompt", token)
        except PermissionError:
            pass
        else:
            raise AssertionError(f"FAILED: run_write accepted read tool {read_tool}")
    check("a read tool cannot be driven through the write path either")


def test_the_write_path_allowlists_exactly_one_tool() -> None:
    print("one tool allowed, every other write tool still denied")
    restore()
    source = Path("helm/claude_cli.py").read_text(encoding="utf-8")
    check("exactly one tool is allowlisted", '"--allowedTools", tool,' in source,
          "run_write must pass a single tool, not a tuple")
    check("every other write tool is still disallowed",
          'others = tuple(t for t in WRITE_TOOLS if t != tool)' in source)
    check("so a send talked into forwarding is refused by the CLI",
          '"--disallowedTools", *others' in source)

    # Read paths must still deny the whole write set.
    check("run_once still disallows all write tools",
          '"--disallowedTools", *WRITE_TOOLS' in source)
    check("and so does the resident session",
          source.count('"--disallowedTools", *WRITE_TOOLS') >= 2,
          str(source.count('"--disallowedTools", *WRITE_TOOLS')))


def test_read_allowlists_never_touch_write_tools() -> None:
    print("no read allowlist contains a write tool")
    writes = set(cli.WRITE_TOOLS)
    for name, tools in (("CALENDAR_READ", cli.CALENDAR_READ),
                        ("MAIL_READ", cli.MAIL_READ),
                        ("SLACK_READ", cli.SLACK_READ)):
        check(f"{name} is clean", not (set(tools) & writes),
              str(set(tools) & writes))
    check("the send tools are in WRITE_TOOLS",
          f"{cli.MS365}outlook_send_mail" in writes
          and f"{cli.SLACK}slack_send_message" in writes
          and f"{cli.MS365}outlook_respond_to_event" in writes)


# ── per-source sends ────────────────────────────────────────────────────────

def test_mail_sends_exactly_what_was_approved() -> None:
    print("mail sends the approved text, unchanged")
    rec = with_recorder()
    try:
        src = MailSource("jose.gonzalez@sailpoint.com", ["sailpoint.com"],
                         cli.Availability())
        draft = a_draft("mail")
        token = ApprovalToken.mint_from_click(draft.id)

        result = src.send(draft.id, token, draft)
        check("it reports success", result["ok"] is True)
        check("one call, not two", len(rec.calls) == 1, str(len(rec.calls)))

        call = rec.calls[0]
        check("the mail send tool was used",
              call["tool"] == f"{cli.MS365}outlook_send_mail", call["tool"])
        check("the recipient is the approved one", draft.to_addr in call["prompt"])
        check("the subject is the approved one", draft.subject in call["prompt"])
        check("the body is the approved one, verbatim", draft.body in call["prompt"])
        check("the prompt says to send it as written",
              "exactly as written" in call["prompt"])
    finally:
        restore()


def test_a_send_without_the_draft_is_refused() -> None:
    print("the draft that was approved must be the draft that goes")
    rec = with_recorder()
    try:
        src = MailSource("me@x.com", ["x.com"], cli.Availability())
        token = ApprovalToken.mint_from_click("d1")

        try:
            src.send("d1", token)
        except SendRefused as e:
            check("no draft means no send", "exact draft" in str(e))
        else:
            raise AssertionError("FAILED: mail sent without the approved draft")

        try:
            src.send("d1", token, Draft(id="d1", item_id="m1", channel="mail",
                                        to_addr="", subject="s", body="b"))
        except SendRefused as e:
            check("an empty recipient is refused", "recipient" in str(e))
        else:
            raise AssertionError("FAILED: mail sent with no recipient")

        check("nothing reached the connector", rec.calls == [])
    finally:
        restore()


def test_slack_posts_rather_than_sends() -> None:
    print("Slack posts, with its own tool")
    rec = with_recorder()
    try:
        src = SlackSource("jose.gonzalez@sailpoint.com", ["sailpoint.com"],
                          cli.Availability())
        draft = a_draft("slack")
        src.send(draft.id, ApprovalToken.mint_from_click(draft.id), draft)
        check("the Slack tool was used",
              rec.calls[0]["tool"] == f"{cli.SLACK}slack_send_message",
              rec.calls[0]["tool"])
        check("addressed to the conversation", "#deal-acme" in rec.calls[0]["prompt"])
        check("and never to a mail address",
              "@sailpoint.com" not in rec.calls[0]["prompt"])
    finally:
        restore()


def test_rsvp_is_its_own_write() -> None:
    print("an RSVP is a write of its own")
    rec = with_recorder()
    try:
        src = CalendarSource("me@x.com", ["x.com"], cli.Availability())
        item = Item(id="c1", source="calendar", kind="event", subject="Deep dive",
                    starts_at="2026-10-20T14:00:00", sender_addr="priya@north.example",
                    rsvp_pending=True)

        src.respond(item, "decline", ApprovalToken.mint_from_click("rsvp-c1-decline"))
        check("the RSVP tool was used",
              rec.calls[0]["tool"] == f"{cli.MS365}outlook_respond_to_event")
        check("the meeting is identified by what was shown",
              "Deep dive" in rec.calls[0]["prompt"])
        check("and the response is explicit", "decline" in rec.calls[0]["prompt"])

        try:
            src.respond(item, "maybe", ApprovalToken.mint_from_click("x"))
        except SendRefused as e:
            check("an invented response is refused", "not an RSVP" in str(e))
        else:
            raise AssertionError("FAILED: calendar accepted response 'maybe'")

        try:
            src.respond(item, "accept", None)
        except SendRefused as e:
            check("an RSVP without a click is refused", "ApprovalToken" in str(e))
        else:
            raise AssertionError("FAILED: calendar RSVP'd without a token")

        try:
            src.send("d1", ApprovalToken.mint_from_click("d1"), a_draft())
        except SendRefused as e:
            check("the calendar still sends no messages", "sends nothing" in str(e))
        else:
            raise AssertionError("FAILED: the calendar sent a message")

        check("only the one real RSVP reached the connector", len(rec.calls) == 1,
              str(len(rec.calls)))
    finally:
        restore()


# ── through the bridge ──────────────────────────────────────────────────────

def live_api(tmp: Path) -> A.Api:
    store = Store(tmp / "helm.db")
    for key, value in DEMO_SETTINGS.items():
        store.set_setting(key, value)
    api = A.Api(store, EventBus(), demo=False)
    return api


def test_approve_send_is_the_only_mint_site() -> None:
    print("the click is the only thing that mints a token")
    source = Path("helm/app.py").read_text(encoding="utf-8")
    mints = [ln.strip() for ln in source.splitlines()
             if "mint_from_click" in ln and not ln.strip().startswith("#")]
    check("there are exactly two mint sites: a send and an RSVP",
          len(mints) == 2, str(mints))
    for line in mints:
        check(f"each is a direct call: {line[:48]}", "ApprovalToken.mint_from_click" in line)

    models = Path("helm/models.py").read_text(encoding="utf-8")
    check("the constructor is the only other way to make one",
          models.count("def mint_from_click") == 1)


def test_sending_through_the_api() -> None:
    print("a send through the bridge is logged exactly once")
    rec = with_recorder()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            api = live_api(Path(d))
            api._store.upsert_item(Item(id="m1", source="mail", kind="email",
                                        subject="Re: Q4 forecast"))
            draft = a_draft("mail")
            api._store.add_draft(draft)

            res = api.approve_send(draft.id)
            check("it sent", res.get("ok") is True, str(res))
            check("one connector call", len(rec.calls) == 1)

            log = api._store.action_log()
            sends = [a for a in log if a["verb"] == "send"]
            check("logged once", len(sends) == 1, str(len(sends)))
            check("approved by click", sends[0]["approved_by"] == "click")
            check("with the exact body", sends[0]["payload"] == draft.body)
            check("and the exact recipient", sends[0]["target"] == draft.to_addr)
            check("status sent", sends[0]["status"] == "sent")

            # The undo window plus a slow connector makes a double-click possible.
            again = api.approve_send(draft.id)
            check("a second click cannot send it twice", "error" in again, str(again))
            check("and no second call was made", len(rec.calls) == 1)
    finally:
        restore()


def test_a_failed_send_says_so_and_is_logged() -> None:
    print("a failure is recorded as a failure, not swallowed")
    rec = with_recorder(cli.ClaudeUnavailable("no confirmation within 180 s"))
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            api = live_api(Path(d))
            api._store.add_draft(a_draft("mail"))
            res = api.approve_send("d1")
            check("the error reaches the UI", "error" in res, str(res))
            check("and names the timeout", "180" in res["error"], res["error"])
            sends = [a for a in api._store.action_log() if a["verb"] == "send"]
            check("the attempt is logged", len(sends) == 1)
            check("as failed", sends[0]["status"] == "failed")
            check("still approved by click", sends[0]["approved_by"] == "click")
            check("the connector was reached once", len(rec.calls) == 1)
    finally:
        restore()


def test_undo_is_recorded() -> None:
    print("undo is an action, not just a cleared timer")
    rec = with_recorder()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            api = live_api(Path(d))
            api._store.add_draft(a_draft("mail"))

            res = api.cancel_send("d1")
            check("undo succeeds", res.get("undone") is True)
            check("nothing was sent", rec.calls == [])
            log = [a for a in api._store.action_log() if a["verb"] == "send"]
            check("it is in the action log", len(log) == 1)
            check("as undone", log[0]["status"] == "undone", log[0]["status"])
            check("with the body you nearly sent", "Wednesday" in log[0]["payload"])
            check("an unknown draft fails cleanly", "error" in api.cancel_send("nope"))
    finally:
        restore()


def test_irreversible_needs_a_second_confirmation() -> None:
    print("risk tier 2 needs a second confirmation")
    rec = with_recorder()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            api = live_api(Path(d))
            # Nothing in HELM deletes anything today. The tier is exercised here so
            # the gate is proven before anything needs it.
            A._RISK["mail"] = "irreversible"
            api._store.add_draft(a_draft("mail"))

            first = api.approve_send("d1")
            check("the first click is not enough",
                  first.get("needs_confirmation") is True, str(first))
            check("and it says why it cannot be undone",
                  "cannot be undone" in first["warning"])
            check("nothing was sent", rec.calls == [])

            second = api.approve_send("d1", confirmed=True)
            check("a confirmed click sends", second.get("ok") is True, str(second))
            check("now it reached the connector", len(rec.calls) == 1)
    finally:
        A._RISK["mail"] = "send"
        restore()


def test_rsvp_through_the_api() -> None:
    print("RSVP through the bridge")
    rec = with_recorder()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            api = live_api(Path(d))
            api._store.upsert_item(Item(
                id="c1", source="calendar", kind="event", subject="Deep dive",
                starts_at="2026-10-20T14:00:00", rsvp_pending=True))

            res = api.approve_rsvp("c1", "accept")
            check("it responded", res.get("ok") is True, str(res))
            check("through the RSVP tool",
                  rec.calls[0]["tool"] == f"{cli.MS365}outlook_respond_to_event")

            log = [a for a in api._store.action_log() if a["verb"] == "rsvp"]
            check("logged", len(log) == 1)
            check("approved by click", log[0]["approved_by"] == "click")
            check("with the response recorded", log[0]["payload"] == "accept")
            check("the meeting is no longer pending",
                  api._store.item("c1").rsvp_pending is False)

            check("a non-meeting is refused", "error" in api.approve_rsvp("nope", "accept"))
    finally:
        restore()


def test_demo_mode_still_sends_nothing() -> None:
    print("demo mode exercises the gate and sends nothing")
    rec = with_recorder()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            store = Store(Path(d) / "helm.db")
            for key, value in DEMO_SETTINGS.items():
                store.set_setting(key, value)
            api = A.Api(store, EventBus(), demo=True)
            api.sync()
            api.chat_send("demo-mail-1", "draft reply")
            import time
            count = "SELECT COUNT(*) AS n FROM draft"
            for _ in range(100):
                if store._conn().execute(count).fetchone()["n"]:
                    break
                time.sleep(0.05)
            row = store._conn().execute("SELECT id FROM draft LIMIT 1").fetchone()
            res = api.approve_send(row["id"])
            check("the gate ran", res.get("ok") is True, str(res))
            check("and demo mode says it sent nothing", res.get("demo") is True)
            check("no connector call was made", rec.calls == [], str(rec.calls))
    finally:
        restore()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for fn in tests:
            fn()
    finally:
        restore()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
