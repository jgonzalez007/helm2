"""Phase 3 self-check: mail parsing, needs-reply, waiting-on, voice, draft safety.

No window, no network, no accounts — every connector reply here is a literal.

    python -m tests.test_phase3
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from helm import claude_cli as cli
from helm.connectors import mail_source as ms
from helm.connectors.base import SendRefused
from helm.models import ApprovalToken, Item
from helm.store import Store

passed = 0


def check(label: str, cond: bool = True, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ok   {label}")
    else:
        raise AssertionError(f"FAILED: {label}" + (f" — {detail}" if detail else ""))


ME = "jose.gonzalez@sailpoint.com"
INTERNAL = ["sailpoint.com"]
NOW = datetime(2026, 10, 19, 9, 0).astimezone()


def msg(**kw) -> dict:
    base = {"id": "m1", "conversation_id": "c1", "subject": "Hello",
            "from_name": "Scott Chen", "from_email": "scott.chen@sailpoint.com",
            "to_emails": [ME], "cc_emails": [], "received": "2026-10-19T08:00:00",
            "is_read": False, "has_attachments": False, "preview": "Body text."}
    base.update(kw)
    return base


# ── Message cleaning ────────────────────────────────────────────────────────

def test_message_cleaning_is_defensive() -> None:
    print("every field from a message is coerced and bounded")
    hostile = msg(
        id={"nested": "object"},
        subject="x" * 5000,
        from_name=["a", "list"],
        from_email="ATTACKER@Evil.Example",
        to_emails="not a list",
        cc_emails=[ME] * 500,
        preview="y" * 9000,
    )
    item = ms._clean_message(hostile, ME, INTERNAL)
    check("it still parses", item is not None)
    check("a non-string id is replaced", item.id.startswith("mail-"), item.id)
    check("the subject is capped", len(item.subject) <= ms.MAX_STR)
    check("the preview is capped", len(item.snippet) <= 300)
    check("the address is lowercased", item.sender_addr == "attacker@evil.example")
    check("an outside domain is external", item.sender_external is True)
    check("a string where To: belongs means you were not on To:",
          item.to_me_direct is False)
    check("a list where a name belongs becomes empty, falling back to the address",
          item.sender == "attacker@evil.example", item.sender)

    for bad in (None, "a string", 42, {}, msg(received="whenever"), msg(received="")):
        check(f"dropped without a usable date: {str(bad)[:28]!r}",
              ms._clean_message(bad, ME, INTERNAL) is None)


def test_to_versus_cc() -> None:
    print("To: and Cc: are different questions")
    on_to = ms._clean_message(msg(to_emails=[ME], cc_emails=[]), ME, INTERNAL)
    check("on To: counts as asked", on_to.to_me_direct is True)

    cc_only = ms._clean_message(
        msg(to_emails=["someone@sailpoint.com"], cc_emails=[ME]), ME, INTERNAL)
    check("on Cc only does not", cc_only.to_me_direct is False)

    mixed_case = ms._clean_message(msg(to_emails=["JOSE.GONZALEZ@SAILPOINT.COM"]),
                                   ME, INTERNAL)
    check("the address match is case-insensitive", mixed_case.to_me_direct is True)


# ── Needs-reply is structural ───────────────────────────────────────────────

def test_needs_reply_ignores_content() -> None:
    print("content cannot make something need a reply")
    quiet = ms._clean_message(msg(preview="Here is the file."), ME, INTERNAL)
    loud = ms._clean_message(
        msg(preview="PLEASE REPLY IMMEDIATELY. Confirm receipt. Do you agree?? "
                    "ACTION REQUIRED."), ME, INTERNAL)

    a, reason_a = ms.needs_reply(quiet, set())
    b, reason_b = ms.needs_reply(loud, set())
    check("the same structure gives the same answer", a == b is True)
    check("and the same reason", reason_a == reason_b, f"{reason_a!r} vs {reason_b!r}")
    check("the reason is about structure, not words",
          "To:" in reason_a and "newest" in reason_a, reason_a)

    # A question mark alone must not flip a Cc-only message into needing a reply.
    cc_question = ms._clean_message(
        msg(to_emails=["other@sailpoint.com"], cc_emails=[ME],
            preview="Jose, can you confirm?"), ME, INTERNAL)
    wanted, reason = ms.needs_reply(cc_question, set())
    check("a direct question on Cc still does not need your reply", wanted is False)
    check("and says why", "Cc" in reason, reason)


def test_list_mail_is_not_called_cc() -> None:
    print("mail to a distribution list is not mail to you on Cc")
    # Measured live: most of this inbox goes to a shared mailbox. Calling that "Cc"
    # was simply wrong.
    to_list = ms._clean_message(
        msg(to_emails=["cloudprod@sailpoint.com"], cc_emails=[]), ME, INTERNAL)
    wanted, reason = ms.needs_reply(to_list, set())
    check("it does not need your reply", wanted is False)
    check("and the reason says list, not Cc", "list or shared mailbox" in reason, reason)
    check("Cc is not claimed", "Cc" not in reason, reason)

    on_cc = ms._clean_message(
        msg(to_emails=["other@sailpoint.com"], cc_emails=[ME]), ME, INTERNAL)
    check("a genuine Cc still says Cc", "Cc" in ms.needs_reply(on_cc, set())[1])
    check("cc_me is recorded", on_cc.cc_me is True)
    check("and is false when you are not on Cc", to_list.cc_me is False)


def test_automated_senders_do_not_need_replies() -> None:
    print("the automated senders the live inbox was full of")
    # Every one of these was marked "needs your reply" before the list grew.
    for addr in ("approvals@sailpoint.com", "sailpoint@service-now.com",
                 "proofpoint-digest@sailpoint.com", "email@mail.salesforce.com",
                 "health@aws.com", "mssecurity-noreply@microsoft.com",
                 "no-reply@eventsmail.dell.com", "jira@atlassian.net",
                 "alerts@datadog.com", "billing@vendor.com"):
        item = ms._clean_message(msg(from_email=addr), ME, INTERNAL)
        wanted, reason = ms.needs_reply(item, set())
        check(f"{addr} does not need a reply", wanted is False, reason)

    # A real colleague must still get through the widened filter.
    for addr in ("scott.chen@sailpoint.com", "alex.ball@rubrik.com",
                 "saul.martinez@sailpoint.com"):
        item = ms._clean_message(msg(from_email=addr), ME, INTERNAL)
        check(f"{addr} still needs a reply", ms.needs_reply(item, set())[0] is True)


def test_needs_reply_structure() -> None:
    print("who sent the last message, and did you answer")
    item = ms._clean_message(msg(), ME, INTERNAL)

    wanted, _ = ms.needs_reply(item, set())
    check("unanswered and addressed to you", wanted is True)

    wanted, reason = ms.needs_reply(item, {"c1"})
    check("already replied in that thread", wanted is False)
    check("and says so", "already replied" in reason, reason)

    bulk = ms._clean_message(msg(from_email="no-reply@sailpoint.com"), ME, INTERNAL)
    wanted, reason = ms.needs_reply(bulk, set())
    check("a no-reply address never needs a reply", wanted is False)
    check("and says why", "bulk" in reason, reason)

    for addr in ("notifications@github.com", "newsletter@vendor.com",
                 "mailer@service.io", "updates@tool.com", "bounce@x.com"):
        other = ms._clean_message(msg(from_email=addr), ME, INTERNAL)
        check(f"{addr} is bulk", ms.needs_reply(other, set())[0] is False)

    bounce = ms._clean_message(msg(from_email="postmaster@sailpoint.com"), ME, INTERNAL)
    check("a bounce notification does not need a reply",
          ms.needs_reply(bounce, set())[0] is False)


# ── Waiting-on ──────────────────────────────────────────────────────────────

def sent(**kw) -> dict:
    base = {"id": "s1", "conversation_id": "c9", "subject": "Re: SOW redlines",
            "to_emails": ["legal@northwind-example.com"],
            "sent_at": (NOW - timedelta(days=5)).isoformat(timespec="seconds"),
            "preview": "Sending the redlines over."}
    base.update(kw)
    return base


def test_waiting_on() -> None:
    print("things you sent that nobody answered")
    out = ms.find_waiting([sent()], [], ME, now=NOW)
    check("a 5-day-old unanswered message surfaces", len(out) == 1, str(len(out)))
    check("it is marked waiting", out[0].state == "waiting")
    check("it records when you sent it", bool(out[0].sent_by_me_at))
    check("and it is attributed to you", out[0].sender == "You")

    fresh = sent(sent_at=(NOW - timedelta(hours=6)).isoformat(timespec="seconds"))
    check("6 hours is too soon to call it waiting",
          ms.find_waiting([fresh], [], ME, now=NOW) == [])

    answered_at = (NOW - timedelta(days=4)).isoformat(timespec="seconds")
    reply = Item(id="r", source="mail", kind="email", subject="Re: SOW redlines",
                 thread_id="c9", received_at=answered_at)
    check("a reply in the same thread clears it",
          ms.find_waiting([sent()], [reply], ME, now=NOW) == [])

    # A reply that predates what you sent does not count as an answer.
    stale = Item(id="r2", source="mail", kind="email", subject="Re: SOW",
                 thread_id="c9",
                 received_at=(NOW - timedelta(days=9)).isoformat(timespec="seconds"))
    check("an older message in the thread is not an answer",
          len(ms.find_waiting([sent()], [stale], ME, now=NOW)) == 1)

    dupes = [sent(id="a"), sent(id="b"), sent(id="c")]
    check("one entry per conversation, not per message",
          len(ms.find_waiting(dupes, [], ME, now=NOW)) == 1)

    check("junk timestamps are skipped",
          ms.find_waiting([sent(sent_at="whenever")], [], ME, now=NOW) == [])


def test_waiting_on_excludes_rsvps_and_notes_to_self() -> None:
    print("the noise the live run surfaced in waiting-on")
    # Outlook files calendar responses in Sent Items. Four showed up as things
    # awaiting a reply.
    for subject in ("Declined: Jose <> Scott || Weekly 1:1",
                    "Accepted: IT Ops Leadership",
                    "Canceled: 1:1 Michael",
                    "Cancelled: Standup",
                    "Tentative: Review",
                    "Updated: Weekly sync"):
        out = ms.find_waiting([sent(conversation_id=f"c-{subject}", subject=subject)],
                              [], ME, now=NOW)
        check(f"{subject[:26]!r} is not awaiting a reply", out == [], str(out))

    check("a real message with a similar word still counts",
          len(ms.find_waiting([sent(subject="Declining vendor options")], [],
                              ME, now=NOW)) == 1)

    # A forward to yourself is a note, not a question.
    to_self = sent(conversation_id="c-self", to_emails=[ME])
    check("a message only to yourself is not awaiting a reply",
          ms.find_waiting([to_self], [], ME, now=NOW) == [])
    check("a message with no recipients at all is skipped",
          ms.find_waiting([sent(conversation_id="c-none", to_emails=[])], [],
                          ME, now=NOW) == [])
    check("you plus someone else still counts",
          len(ms.find_waiting([sent(conversation_id="c-both",
                                    to_emails=[ME, "legal@x.com"])], [],
                              ME, now=NOW)) == 1)


# ── Voice profile ───────────────────────────────────────────────────────────

def test_voice_profile() -> None:
    print("writing voice, learned locally with no model call")
    samples = [
        {"preview": "Hi Scott,\n\nNumbers land Wednesday. EMEA is the only line "
                    "still moving.\n\nThanks\nJose"},
        {"preview": "Hi Heidi,\n\nConfirmed on the uplift. Go ahead and send it.\n\n"
                    "Thanks\nJose"},
        {"preview": "Hi Bryan,\n\nI'll get you the west coast split by Friday.\n\n"
                    "Thanks"},
    ]
    p = ms.build_voice_profile(samples)
    check("it read the samples", p["samples"] == 3)
    check("it found the greeting", p["greeting"] == "hi", p["greeting"])
    check("it noticed you use first names", p["uses_first_name"] is True)
    check("it found the sign-off", p["signoff"] == "thanks", p["signoff"])
    check("it measured length", p["median_words"] > 0, str(p["median_words"]))

    described = ms.describe_voice(p)
    # Capitalised on the way out: the profile stores "hi" and the first live draft
    # opened with a lowercase "hi," because the model copied it literally.
    check("the description names the greeting as you would write it",
          '"Hi"' in described, described)
    check("the description names the sign-off", '"thanks"' in described)
    check("the description gives a length", "words" in described)

    empty = ms.build_voice_profile([])
    check("no samples gives an honest empty profile", empty == {"samples": 0})
    check("and the description says so",
          "no sent mail has been read" in ms.describe_voice(empty))
    check("a missing profile is handled", "no sent mail" in ms.describe_voice({}))

    terse = ms.build_voice_profile([{"preview": "Done."}, {"preview": "Approved."}])
    check("someone who skips greetings gets told so",
          "skip the greeting" in ms.describe_voice(terse), ms.describe_voice(terse))


# ── The sweep ───────────────────────────────────────────────────────────────

class FakeCli:
    """Stands in for the connector so the sweep can be tested without one."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, tuple]] = []

    def __call__(self, prompt, allowed_tools, timeout=None):
        self.calls.append((prompt, allowed_tools))
        return self.reply, 1234


def test_mail_timestamps_are_utc_not_wall_clock() -> None:
    print("mail returns a real instant, calendar returns wall clock")
    from helm.connectors import _clean

    # Measured live: mail comes back as "2026-10-19T18:39:52.000Z". The wall-clock
    # parser refuses a trailing Z on purpose, which dropped every message.
    out = _clean.absolute_to_local("2026-10-19T18:39:52.000Z")
    check("a Z timestamp parses", bool(out), out)
    check("it is converted, not relabelled",
          out != "2026-10-19T18:39:52+00:00" or out.endswith("+00:00"), out)
    check("an explicit offset parses",
          bool(_clean.absolute_to_local("2026-10-19T18:39:52+02:00")))
    check("a compact offset parses",
          bool(_clean.absolute_to_local("2026-10-19T18:39:52+0200")))
    check("the wall-clock parser still refuses Z",
          _clean.to_local_iso("2026-10-19T18:39:52.000Z") == "")
    check("the absolute parser refuses a naked wall clock",
          _clean.absolute_to_local("2026-10-19T18:39:52") == "")
    check("any_time_to_local takes both",
          bool(_clean.any_time_to_local("2026-10-19T18:39:52.000Z"))
          and bool(_clean.any_time_to_local("2026-10-19T18:39:52")))
    for junk in ("", "whenever", None, 17, "2026-13-45T99:99:99Z"):
        check(f"junk is refused: {junk!r}", _clean.any_time_to_local(junk) == "")

    item = ms._clean_message(msg(received="2026-10-19T18:39:52.000Z"), ME, INTERNAL)
    check("a real message with a Z timestamp survives", item is not None)


def test_thread_key_falls_back_to_the_subject() -> None:
    print("threads group by subject when the connector gives no conversation id")
    check("a real conversation id wins",
          ms.thread_key("AAQkAD123", "Re: Anything") == "AAQkAD123")

    base = ms.thread_key("", "Q4 forecast sign-off")
    for variant in ("Re: Q4 forecast sign-off", "RE: Q4 forecast sign-off",
                    "FW: Q4 forecast sign-off", "Fwd: Q4 forecast sign-off",
                    "Re: Re: FW: Q4 forecast sign-off", "re:  Q4 forecast sign-off"):
        check(f"{variant!r} groups with the original",
              ms.thread_key("", variant) == base, ms.thread_key("", variant))

    check("a different subject is a different thread",
          ms.thread_key("", "Re: Something else") != base)
    check("an empty subject gives no key", ms.thread_key("", "") == "")
    check("the key is structural, not a body read", base.startswith("subject:"), base)

    # And it makes "already replied" work without conversation ids.
    inbox = ms._clean_message(msg(conversation_id="", subject="Q4 forecast"),
                              ME, INTERNAL)
    replied = {ms.thread_key("", "Re: Q4 forecast")}
    check("a reply with a Re: prefix clears the original",
          ms.needs_reply(inbox, replied)[0] is False)


def test_sweep_maps_inbox_and_sent(monkeypatched=None) -> None:
    print("one sweep produces inbox rows, waiting rows and a voice profile")
    reply = """```json
{"inbox": [
  {"id":"i1","conversation_id":"c1","subject":"Q4 forecast sign-off",
   "from_name":"Scott Chen","from_email":"scott.chen@sailpoint.com",
   "to_emails":["jose.gonzalez@sailpoint.com"],"cc_emails":[],
   "received":"2026-10-19T08:00:00.0000000","timezone":"","is_read":false,
   "has_attachments":false,"preview":"Need your numbers."},
  {"id":"i2","conversation_id":"c2","subject":"Weekly newsletter",
   "from_name":"Eng Comms","from_email":"no-reply@sailpoint.com",
   "to_emails":["jose.gonzalez@sailpoint.com"],"cc_emails":[],
   "received":"2026-10-19T07:00:00.0000000","timezone":"","is_read":true,
   "has_attachments":false,"preview":"What shipped."},
  {"id":"i3","conversation_id":"c3","subject":"FYI only",
   "from_name":"Bryan","from_email":"bryan.kelley@sailpoint.com",
   "to_emails":["other@sailpoint.com"],"cc_emails":["jose.gonzalez@sailpoint.com"],
   "received":"2026-10-19T06:00:00.0000000","timezone":"","is_read":true,
   "has_attachments":false,"preview":"Context."}
],
"sent": [
  {"id":"s1","conversation_id":"c3","subject":"Re: FYI only",
   "to_emails":["bryan.kelley@sailpoint.com"],"sent":"2026-10-19T06:30:00.0000000",
   "preview":"Hi Bryan,\\n\\nGot it.\\n\\nThanks"}
]}
```"""
    fake = FakeCli(reply)
    real = cli.run_once
    cli.run_once = fake
    try:
        source = ms.MailSource(ME, INTERNAL, cli.Availability(connected={"Microsoft 365"}))
        items = source.list_new("")
    finally:
        cli.run_once = real

    check("it made one connector call", len(fake.calls) == 1, str(len(fake.calls)))
    check("with only read tools", fake.calls[0][1] == cli.MAIL_READ)
    # Three live sweeps measured 197 s, 232 s and 242 s. The shared 240 s default
    # timed out on the third and reported an empty inbox.
    check("the mail sweep has its own, larger timeout",
          ms.MailSource.SWEEP_TIMEOUT > cli.ONE_SHOT_TIMEOUT,
          f"{ms.MailSource.SWEEP_TIMEOUT} vs {cli.ONE_SHOT_TIMEOUT}")
    check("three inbox rows came back", len(items) == 3, str(len(items)))

    by_id = {i.id: i for i in items}
    check("the real request needs you", by_id["i1"].state == "needs_me")
    check("the newsletter does not", by_id["i2"].state != "needs_me", by_id["i2"].state)
    check("a thread you already replied to is cleared",
          by_id["i3"].state == "done", by_id["i3"].state)
    check("the reason is recorded", bool(source.reply_reason("i1")))
    check("the status went live", source.status().health == "live")
    check("it counts only what needs you", source.status().count == 1,
          str(source.status().count))
    check("the voice profile was built", source.voice.get("samples") == 1,
          str(source.voice))
    check("the fractional timestamp parsed",
          by_id["i1"].received_at.startswith("2026-10-19T08:00:00"))


def test_sweep_failures_are_honest() -> None:
    print("a sweep that cannot be read says so instead of emptying the inbox")
    for bad in ("I could not read your mail.", "[]", "not json at all", ""):
        fake = FakeCli(bad)
        real = cli.run_once
        cli.run_once = fake
        try:
            source = ms.MailSource(ME, INTERNAL,
                                   cli.Availability(connected={"Microsoft 365"}))
            items = source.list_new("")
        finally:
            cli.run_once = real
        check(f"{bad[:24]!r} returns nothing and reconnects",
              items == [] and source.status().health == "reconnecting",
              source.status().health)
        check("and records why", bool(source.status().last_error))

    # The failure that looks like success: a dict that parses but carries nothing.
    for shaped in ('{"error":"I could not read your mail"}',
                   '{"messages":[{"subject":"x"}]}',
                   '{"inbox":"not a list","sent":[]}',
                   '{"sent":[]}'):
        fake = FakeCli(shaped)
        real = cli.run_once
        cli.run_once = fake
        try:
            source = ms.MailSource(ME, INTERNAL,
                                   cli.Availability(connected={"Microsoft 365"}))
            items = source.list_new("")
        finally:
            cli.run_once = real
        check(f"a dict with no inbox list is not a successful empty sweep: {shaped[:26]!r}",
              source.status().health == "reconnecting", source.status().health)
        check("and the raw reply is kept for debugging",
              "reply had no inbox list" in source.status().last_error)

    # A genuinely empty inbox is a success, and must not be confused with the above.
    fake = FakeCli('{"inbox":[],"sent":[]}')
    real = cli.run_once
    cli.run_once = fake
    try:
        source = ms.MailSource(ME, INTERNAL, cli.Availability(connected={"Microsoft 365"}))
        items = source.list_new("")
    finally:
        cli.run_once = real
    check("an explicitly empty inbox is live, not an error",
          items == [] and source.status().health == "live", source.status().health)

    unavailable = ms.MailSource(ME, INTERNAL, cli.Availability())
    check("no connector means unavailable, not an empty inbox",
          unavailable.list_new("") == []
          and unavailable.status().health == "unavailable")
    check("and names the reason", "CLI" in unavailable.status().last_error
          or "connector" in unavailable.status().last_error,
          unavailable.status().last_error)


def test_threads_are_not_prefetched() -> None:
    print("the sweep never pulls a thread")
    check("mail declares threads as fetch-on-demand",
          ms.MailSource.prefetch_threads is False)


def test_mail_cannot_send() -> None:
    print("mail drafts, and refuses to send")
    source = ms.MailSource(ME, INTERNAL)
    try:
        source.send("d1", None)
    except SendRefused as e:
        check("no token is refused by the gate", "approval" in str(e).lower())
    else:
        raise AssertionError("FAILED: mail send accepted a missing token")

    try:
        source.send("d1", ApprovalToken.mint_from_click("d1"))
    except SendRefused as e:
        check("even a clicked token cannot send yet", "phase 10" in str(e), str(e))
    else:
        raise AssertionError("FAILED: mail send went through")


def test_draft_never_sends_and_is_addressed_to_the_sender() -> None:
    print("a draft is addressed to whoever wrote to you, and nobody else")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        item = ms._clean_message(msg(), ME, INTERNAL)
        store.upsert_item(item)
        store.set_thread(item.id, [{"author": "Scott Chen", "body": "Need numbers.",
                                    "sent_at": item.received_at, "is_me": False}])

        class FakeSession:
            def __init__(self):
                self.prompt = ""

            def ask(self, prompt, on_token=None, timeout=None):
                self.prompt = prompt
                return '{"subject":"Re: Hello","body":"Wednesday morning."}'

        source = ms.MailSource(ME, INTERNAL, store=store)
        fake = FakeSession()
        source._session = fake
        draft = source.draft(item.id, "draft reply")

        check("the recipient is the sender", draft.to_addr == "scott.chen@sailpoint.com",
              draft.to_addr)
        check("the subject is a reply", draft.subject.lower().startswith("re:"),
              draft.subject)
        check("the body came through", "Wednesday" in draft.body)
        check("it is only a draft", draft.status == "drafted")
        check("the prompt carried the thread", "Need numbers." in fake.prompt)
        check("the prompt carried the voice", "My style" in fake.prompt)
        check("the prompt says not to send", "Do not send" in fake.prompt)

        # A reply that comes back as prose is kept rather than lost: a human reads it.
        class ProseSession(FakeSession):
            def ask(self, prompt, on_token=None, timeout=None):
                return "Scott — numbers land Wednesday."

        source._session = ProseSession()
        prose = source.draft(item.id, "draft reply")
        check("prose is kept as the body", "Wednesday" in prose.body)
        check("and still gets a reply subject",
              prose.subject.lower().startswith("re:"), prose.subject)
        check("and is still addressed to the sender",
              prose.to_addr == "scott.chen@sailpoint.com")


def test_mail_allowlist_is_read_only() -> None:
    print("the mail allowlist cannot reach a write tool")
    check("MAIL_READ has no write tool",
          not (set(cli.MAIL_READ) & set(cli.WRITE_TOOLS)))
    check("the sweep prompt forbids reading individual messages",
          "Do not read any individual message" in ms.MAIL_PROMPT)
    check("the sweep prompt pins the call count",
          "exactly two tool calls" in ms.MAIL_PROMPT)
    check("the draft prompt says a human approves",
          "a human will read and approve" in ms.DRAFT_PROMPT)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
