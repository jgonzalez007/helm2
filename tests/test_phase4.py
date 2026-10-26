"""Phase 4 self-check: Slack parsing, mentions vs broadcasts, needs-reply, drafts.

The distinction worth breaking a build over: `<!here>` is not a mention of you.

    python -m tests.test_phase4
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from helm import claude_cli as cli
from helm.connectors import slack_source as ss
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


ME_ID = "U080ANG97TM"
ME_HANDLE = "jose.gonzalez"
ME_ADDR = "jose.gonzalez@sailpoint.com"
INTERNAL = ["sailpoint.com"]


def dm(**kw) -> dict:
    base = {"id": "D0C9:1792091911.993409", "channel": "DM with Atlee Chalwell",
            "channel_id": "D0C9LSSU02W", "channel_type": "im",
            "author": "Atlee Chalwell", "author_handle": "atlee.chalwell",
            "author_id": "U07T37755MX", "text": "Is it possible to share the posting?",
            "sent_at": "2026-10-15T14:18:31-05:00", "is_me": False}
    base.update(kw)
    return base


# ── A broadcast is not a mention ────────────────────────────────────────────

def test_here_is_not_a_mention() -> None:
    print("<!here> is addressed to the room, not to you")
    # The only "mention" in a real three-day search was exactly this.
    broadcast = "<!here> Team,\n\nI created the new slack channel for the IT team"
    check("a broadcast does not mention you", ss.mentions_me(broadcast, ME_ID) is False)
    check("but it is recognised as a broadcast", ss.is_broadcast(broadcast) is True)

    for tag in ("<!channel>", "<!everyone>", "<!here>"):
        check(f"{tag} is a broadcast", ss.is_broadcast(f"{tag} please look"))
        check(f"{tag} is not a mention", ss.mentions_me(f"{tag} please look", ME_ID) is False)

    direct = f"<@{ME_ID}> can you confirm the uplift?"
    check("your own id is a mention", ss.mentions_me(direct, ME_ID) is True)
    check("and it is not a broadcast", ss.is_broadcast(direct) is False)

    other = "<@U07T37755MX> can you confirm?"
    check("somebody else's id is not your mention",
          ss.mentions_me(other, ME_ID) is False)
    check("a mention with a trailing label still counts",
          ss.mentions_me(f"<@{ME_ID}|jose> hi", ME_ID) is True)
    check("no id configured means no mention", ss.mentions_me(direct, "") is False)


def test_broadcast_does_not_reach_the_focus_queue() -> None:
    print("a channel announcement is not addressed to you")
    item = ss._clean_message(
        dm(channel_type="channel", channel="it-ai",
           text="<!here> Team, exploring the Slack AI path"), ME_ID, INTERNAL)
    check("it parses", item is not None)
    check("it is not a direct ping", item.to_me_direct is False)
    check("and its kind is not 'mention'", item.kind != "mention", item.kind)

    named = ss._clean_message(
        dm(channel_type="channel", channel="it-ai",
           text=f"<@{ME_ID}> can you take this one?"), ME_ID, INTERNAL)
    check("a named mention is addressed to you", named.to_me_direct is True)
    check("and its kind is 'mention'", named.kind == "mention", named.kind)

    from helm.flags import Settings, classify
    cfg = Settings(internal_domains={"sailpoint.com"})
    check("a direct mention is important", classify(named, cfg).importance == "high")
    check("a broadcast is not", classify(item, cfg).importance != "high",
          classify(item, cfg).importance)


# ── Markup ──────────────────────────────────────────────────────────────────

def test_markup_becomes_readable() -> None:
    print("Slack markup is flattened for a list row")
    check("a user ref becomes a name when known",
          ss.to_plain(f"<@{ME_ID}> hi", {ME_ID: "jose"}) == "@jose hi")
    check("and falls back to the id when not",
          ss.to_plain(f"<@{ME_ID}> hi") == f"@{ME_ID} hi")
    check("a channel ref becomes a name",
          ss.to_plain("see <#C123ABC|deal-acme>") == "see #deal-acme")
    check("a channel ref without a name keeps the id",
          ss.to_plain("see <#C123ABC>") == "see #C123ABC")
    check("a labelled link becomes its label",
          ss.to_plain("read <https://x.com/doc|the doc>") == "read the doc")
    check("a bare link stays a link",
          ss.to_plain("read <https://x.com/doc>") == "read https://x.com/doc")
    check("a broadcast reads as @here", ss.to_plain("<!here> team") == "@here team")
    check("newlines collapse", ss.to_plain("a\n\n  b") == "a b")
    check("empty text is handled", ss.to_plain("") == "")
    check("a non-string is handled", ss.to_plain(None) == "")


# ── Addressing ──────────────────────────────────────────────────────────────

def test_handle_becomes_an_address() -> None:
    print("a Slack handle maps onto the one VIP list")
    check("handle plus the primary internal domain",
          ss.handle_to_address("sree.kancharla", INTERNAL) == "sree.kancharla@sailpoint.com")
    check("a leading @ is stripped",
          ss.handle_to_address("@sree.kancharla", INTERNAL) == "sree.kancharla@sailpoint.com")
    check("case is normalised",
          ss.handle_to_address("Sree.Kancharla", INTERNAL) == "sree.kancharla@sailpoint.com")
    check("no handle gives no address", ss.handle_to_address("", INTERNAL) == "")
    check("no domain gives no address", ss.handle_to_address("sree", []) == "")

    # And it makes a manager list work across both sources.
    from helm.flags import Settings, classify
    cfg = Settings(managers={"scott.chen@sailpoint.com"},
                   internal_domains={"sailpoint.com"})
    item = ss._clean_message(dm(author_handle="scott.chen", author="Scott Chen"),
                             ME_ID, INTERNAL)
    check("a manager on Slack is important too",
          classify(item, cfg).importance == "high")
    check("for the manager-list reason",
          any(r["rule"] == "manager" for r in classify(item, cfg).reasons))


def test_message_cleaning_is_defensive() -> None:
    print("every field from Slack is coerced and bounded")
    hostile = dm(id={"nested": "object"}, channel=["a", "list"],
                 author=["also", "a", "list"], author_handle="x" * 500,
                 text="y" * 9000)
    item = ss._clean_message(hostile, ME_ID, INTERNAL)
    check("it still parses", item is not None)
    check("a non-string id is replaced", ":" in item.id, item.id)
    check("the snippet is capped", len(item.snippet) <= 1000)
    check("a list where a name belongs falls back to the handle",
          item.sender.startswith("x"), item.sender[:20])

    for bad in (None, "a string", 42, {}, dm(sent_at="whenever"), dm(sent_at="")):
        check(f"dropped without a usable timestamp: {str(bad)[:26]!r}",
              ss._clean_message(bad, ME_ID, INTERNAL) is None)

    check("the offset timestamp converts to local",
          ss._clean_message(dm(), ME_ID, INTERNAL).received_at.startswith("2026-10-15T"))

    # The live sweep returned "2026-10-15 23:30:56 CDT" — a space separator and a zone
    # abbreviation. Both earlier parsers rejected it, which dropped every message.
    from helm.connectors import _clean

    local, warning = _clean.abbrev_to_local("2026-10-15 23:30:56 CDT")
    check("an abbreviation timestamp parses", bool(local), local)
    check("the wall clock is preserved", local.startswith("2026-10-15T23:30:56"), local)
    check("a T separator works too",
          bool(_clean.abbrev_to_local("2026-10-15T23:30:56 CDT")[0]))
    check("seconds are optional", bool(_clean.abbrev_to_local("2026-10-15 23:30 CDT")[0]))
    check("an unfamiliar zone is reported, not guessed at",
          bool(_clean.abbrev_to_local("2026-10-15 23:30:56 JST")[1]))
    for junk in ("", "whenever", None, "2026-10-15 23:30:56", "2026-10-15 23:30:56 Z"):
        check(f"not an abbreviation timestamp: {junk!r}",
              _clean.abbrev_to_local(junk)[0] == "")

    check("any_time_to_local takes the abbreviation shape",
          bool(_clean.any_time_to_local("2026-10-15 23:30:56 CDT")))
    live = ss._clean_message(dm(sent_at="2026-10-15 23:30:56 CDT"), ME_ID, INTERNAL)
    check("so a real Slack message survives", live is not None)
    check("with its own time", live.received_at.startswith("2026-10-15T23:30:56"),
          live.received_at)


# ── Needs-reply ─────────────────────────────────────────────────────────────

def msg(key: str, thread: str, sender: str, when: str, kind: str = "dm") -> Item:
    return Item(id=key, source="slack", kind=kind, subject=thread, sender=sender,
                sender_addr=f"{sender}@sailpoint.com", thread_id=thread,
                received_at=when, to_me_direct=(kind in ("dm", "mention")))


def test_needs_reply_is_about_who_spoke_last() -> None:
    print("who spoke last in the conversation")
    older = msg("a", "D1", "atlee.chalwell", "2026-10-15T14:18:31-05:00")
    newer = msg("b", "D1", "atlee.chalwell", "2026-10-15T14:23:35-05:00")
    newest = ss.newest_per_conversation([older, newer])
    check("the newest message is the one that counts", newest["D1"].id == "b")

    wanted, reason = ss.needs_reply(newer, newest, ME_ADDR)
    check("their newest DM needs a reply", wanted is True)
    check("and says why", "not yours" in reason, reason)

    wanted, reason = ss.needs_reply(older, newest, ME_ADDR)
    check("an older message in the same DM does not", wanted is False)
    check("and says it was superseded", "later message" in reason, reason)

    mine = msg("c", "D2", ME_HANDLE, "2026-10-15T15:00:00-05:00")
    mine.sender_addr = ME_ADDR
    wanted, reason = ss.needs_reply(mine, ss.newest_per_conversation([mine]), ME_ADDR)
    check("a conversation where you spoke last does not", wanted is False)
    check("and says so", "you spoke last" in reason, reason)

    channel = msg("d", "C1", "sree.kancharla", "2026-10-15T15:00:00-05:00",
                  kind="thread")
    wanted, reason = ss.needs_reply(channel, ss.newest_per_conversation([channel]),
                                    ME_ADDR)
    check("a channel message that does not name you does not", wanted is False)
    check("and says why", "does not name you" in reason, reason)

    named = msg("e", "C2", "sree.kancharla", "2026-10-15T15:00:00-05:00",
                kind="mention")
    check("one that names you does",
          ss.needs_reply(named, ss.newest_per_conversation([named]), ME_ADDR)[0] is True)


def test_a_stale_conversation_is_not_waiting_on_you() -> None:
    print("age bounds the queue")
    from datetime import datetime, timedelta

    now = datetime(2026, 10, 19, 9, 0).astimezone()

    def at(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    # The first live sweep queued a group DM whose newest message was six months old.
    old = msg("x", "D9", "scott.boysel", at(188))
    wanted, reason = ss.needs_reply(old, ss.newest_per_conversation([old]), ME_ADDR,
                                     now=now)
    check("a six-month-old conversation is not awaiting you", wanted is False)
    check("and the reason gives the age", "days" in reason, reason)

    fresh = msg("y", "D8", "scott.boysel", at(2))
    check("a two-day-old one still is",
          ss.needs_reply(fresh, ss.newest_per_conversation([fresh]), ME_ADDR,
                         now=now)[0] is True)

    edge = msg("z", "D7", "scott.boysel", at(ss.STALE_DAYS - 1))
    check("just inside the window counts",
          ss.needs_reply(edge, ss.newest_per_conversation([edge]), ME_ADDR,
                         now=now)[0] is True)
    past = msg("w", "D6", "scott.boysel", at(ss.STALE_DAYS + 1))
    check("just outside it does not",
          ss.needs_reply(past, ss.newest_per_conversation([past]), ME_ADDR,
                         now=now)[0] is False)


def test_a_group_conversation_between_others_is_not_yours() -> None:
    print("in a room with several people, their chat is not your reply")
    from datetime import datetime, timedelta

    now = datetime(2026, 10, 19, 9, 0).astimezone()

    def gmsg(key, sender, minutes_ago):
        item = msg(key, "G1", sender,
                   (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"))
        item.group_chat = True
        return item

    # Two other people talking: Sree asks Saketh to call, Saketh says "sure".
    ask = gmsg("a", "sree.kancharla", 20)
    reply = gmsg("b", "saketh.chandra", 10)
    convo = [ask, reply]
    newest = ss.newest_per_conversation(convo)

    wanted, reason = ss.needs_reply(reply, newest, ME_ADDR, None, convo, now)
    check("a group exchange you are not in is not yours", wanted is False)
    check("and says so", "not joined" in reason, reason)

    # Now you are part of it, and they replied after you.
    mine = gmsg("c", ME_HANDLE, 15)
    mine.sender_addr = ME_ADDR
    joined = [ask, mine, reply]
    newest2 = ss.newest_per_conversation(joined)
    wanted, reason = ss.needs_reply(reply, newest2, ME_ADDR, None, joined, now)
    check("a group that replied after you is yours", wanted is True)
    check("and says so", "after you" in reason, reason)

    # You spoke last: nothing owed.
    last_word = gmsg("d", ME_HANDLE, 5)
    last_word.sender_addr = ME_ADDR
    after = [ask, reply, last_word]
    wanted, reason = ss.needs_reply(last_word, ss.newest_per_conversation(after),
                                     ME_ADDR, None, after, now)
    check("your own last word is not awaiting you", wanted is False)

    # A one-to-one DM is unaffected by the group rule.
    solo = msg("e", "D5", "atlee.chalwell",
               (now - timedelta(minutes=5)).isoformat(timespec="seconds"))
    check("a 1:1 DM still needs a reply",
          ss.needs_reply(solo, ss.newest_per_conversation([solo]), ME_ADDR,
                         None, [solo], now)[0] is True)


def test_needs_reply_uses_learned_senders() -> None:
    print("a bot you have muted stays muted on Slack too")
    bot = msg("f", "D3", "jira.bot", "2026-10-15T15:00:00-05:00")
    newest = ss.newest_per_conversation([bot])
    check("by default a DM needs a reply",
          ss.needs_reply(bot, newest, ME_ADDR)[0] is True)

    learned = {"jira.bot@sailpoint.com": {"is_automated": True,
                                          "reason": "an app, not a person"}}
    wanted, reason = ss.needs_reply(bot, newest, ME_ADDR, learned)
    check("once learned, it does not", wanted is False)
    check("with the learned reason", reason == "an app, not a person", reason)

    pinned = {"jira.bot@sailpoint.com": {"expects_reply": False, "pinned": True,
                                         "reason": "you muted this one"}}
    check("your click is honoured",
          ss.needs_reply(bot, newest, ME_ADDR, pinned)[0] is False)


# ── The sweep ───────────────────────────────────────────────────────────────

class FakeCli:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple] = []

    def __call__(self, prompt, allowed_tools, timeout=None):
        self.calls.append((prompt, allowed_tools, timeout))
        return self.reply, 4321


def run_sweep(reply: str, store=None):
    fake = FakeCli(reply)
    real = cli.run_once
    cli.run_once = fake
    try:
        source = ss.SlackSource(ME_ADDR, INTERNAL,
                                cli.Availability(connected={"Slack"}), store=store)
        return source, source.list_new(""), fake
    finally:
        cli.run_once = real


def test_sweep() -> None:
    print("one sweep turns three tool calls into rows")
    reply = """{
  "me": {"id": "U080ANG97TM", "handle": "jose.gonzalez", "real_name": "Jose Gonzalez"},
  "dms": [
    {"id":"D1:2","channel":"DM with Atlee Chalwell","channel_id":"D1",
     "channel_type":"im","author":"Atlee Chalwell","author_handle":"atlee.chalwell",
     "author_id":"U07","text":"Did you see the posting?",
     "sent_at":"2026-10-15T14:23:35-05:00","is_me":false},
    {"id":"D1:1","channel":"DM with Atlee Chalwell","channel_id":"D1",
     "channel_type":"im","author":"Jose Gonzalez","author_handle":"jose.gonzalez",
     "author_id":null,"text":"it is currently frozen",
     "sent_at":"2026-10-15T14:19:02-05:00","is_me":true},
    {"id":"D2:1","channel":"DM with Heidi Stark","channel_id":"D2",
     "channel_type":"im","author":"Jose Gonzalez","author_handle":"jose.gonzalez",
     "author_id":null,"text":"sent the redlines",
     "sent_at":"2026-10-15T09:00:00-05:00","is_me":true}
  ],
  "mentions": [
    {"id":"C1:1","channel":"it-ai","channel_id":"C1","channel_type":"channel",
     "author":"Sree Kancharla","author_handle":"sree.kancharla","author_id":"U05",
     "text":"<!here> Team, exploring the Slack AI path",
     "sent_at":"2026-10-16T10:00:00-05:00","permalink":"https://x.slack.com/1"},
    {"id":"C2:1","channel":"deal-acme","channel_id":"C2","channel_type":"channel",
     "author":"Heidi Stark","author_handle":"heidi.stark","author_id":"U09",
     "text":"<@U080ANG97TM> can you confirm the ELA uplift?",
     "sent_at":"2026-10-16T11:00:00-05:00","permalink":"https://x.slack.com/2"}
  ]
}"""
    source, items, fake = run_sweep(reply)

    check("one connector call", len(fake.calls) == 1)
    check("with only Slack read tools", fake.calls[0][1] == cli.SLACK_READ)
    check("and its own larger timeout", fake.calls[0][2] == ss.SlackSource.SWEEP_TIMEOUT)
    check("the sweep timeout exceeds the shared default",
          ss.SlackSource.SWEEP_TIMEOUT > cli.ONE_SHOT_TIMEOUT)
    check("it learned who you are", source.my_id == ME_ID, source.my_id)
    check("and your handle", source.my_handle == ME_HANDLE)
    check("the status went live", source.status().health == "live")

    by_id = {i.id: i for i in items}
    waiting_on_you = [i for i in items if i.state == "needs_me"]
    subjects = sorted(i.subject for i in waiting_on_you)
    check("the unanswered DM needs you",
          "DM with Atlee Chalwell" in subjects, str(subjects))
    check("the direct mention needs you", "#deal-acme" in subjects, str(subjects))
    check("the broadcast does not", "#it-ai" not in subjects, str(subjects))
    check("the DM where you spoke last does not",
          "DM with Heidi Stark" not in subjects, str(subjects))
    check("so exactly two things need you", len(waiting_on_you) == 2,
          str(len(waiting_on_you)))

    check("the count matches", source.status().count == 2, str(source.status().count))
    check("a reason is recorded for each",
          all(source.reply_reason(i.id) for i in waiting_on_you))
    check("markup is flattened for display",
          "@here" in by_id["slack-C1:1"].snippet, by_id["slack-C1:1"].snippet)
    check("a handle became an address",
          by_id["slack-D1:2"].sender_addr == "atlee.chalwell@sailpoint.com")


def test_sweep_builds_the_thread() -> None:
    print("the sweep's own messages are the thread, so the drawer needs no call")
    reply = """{"me":{"id":"U080ANG97TM","handle":"jose.gonzalez"},
  "dms":[
    {"id":"D1:2","channel":"DM","channel_id":"D1","channel_type":"im",
     "author":"Atlee","author_handle":"atlee","text":"second",
     "sent_at":"2026-10-15T14:23:35-05:00","is_me":false},
    {"id":"D1:1","channel":"DM","channel_id":"D1","channel_type":"im",
     "author":"Atlee","author_handle":"atlee","text":"first",
     "sent_at":"2026-10-15T14:18:31-05:00","is_me":false}],
  "mentions":[]}"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        source, items, _ = run_sweep(reply, store)
        for item in items:
            store.upsert_item(item)
        check("threads are prefetched, not fetched on open",
              ss.SlackSource.prefetch_threads is True)
        thread = source.get_thread("slack-D1:2")
        check("the thread has both messages", len(thread.messages) == 2,
              str(len(thread.messages)))
        check("oldest first", thread.messages[0]["body"] == "first",
              thread.messages[0]["body"])


def test_sweep_failures_are_honest() -> None:
    print("a reply we cannot read is not a quiet Slack")
    for bad in ('{"error":"permission denied"}', '{"me":{},"mentions":[]}',
                '{"dms":"not a list"}', "not json", "", "[]"):
        source, items, _ = run_sweep(bad)
        check(f"{bad[:26]!r} reconnects instead of emptying",
              items == [] and source.status().health == "reconnecting",
              source.status().health)
        check("and records why", bool(source.status().last_error))

    source, items, _ = run_sweep('{"me":{},"dms":[],"mentions":[]}')
    check("an explicitly empty Slack is live, not an error",
          items == [] and source.status().health == "live", source.status().health)

    offline = ss.SlackSource(ME_ADDR, INTERNAL, cli.Availability())
    check("no connector means unavailable",
          offline.list_new("") == [] and offline.status().health == "unavailable")
    check("and names the reason", bool(offline.status().last_error))


# ── Drafting and the gate ───────────────────────────────────────────────────

def test_slack_cannot_send() -> None:
    print("Slack drafts, and refuses to post")
    source = ss.SlackSource(ME_ADDR, INTERNAL)
    try:
        source.send("d1", None)
    except SendRefused as e:
        check("no token is refused by the gate", "approval" in str(e).lower())
    else:
        raise AssertionError("FAILED: Slack send accepted a missing token")

    try:
        source.send("d1", ApprovalToken.mint_from_click("d1"))
    except SendRefused as e:
        # Phase 10 wired posting. The approved draft must come with the token.
        check("a clicked token alone is not enough", "exact draft" in str(e), str(e))
    else:
        raise AssertionError("FAILED: Slack post went through on a token alone")


def test_draft_goes_back_to_the_conversation() -> None:
    print("a Slack draft is addressed to the conversation, never to a handle")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(Path(d) / "t.db")
        item = ss._clean_message(dm(channel_type="channel", channel="deal-acme",
                                     text=f"<@{ME_ID}> confirm the uplift?"),
                                  ME_ID, INTERNAL)
        store.upsert_item(item)
        store.set_thread(item.id, [{"author": "Heidi", "body": "confirm the uplift?",
                                    "sent_at": item.received_at, "is_me": False}])

        class FakeSession:
            def __init__(self):
                self.prompt = ""

            def ask(self, prompt, on_token=None, timeout=None):
                self.prompt = prompt
                return '{"body":"Confirmed - go ahead and send the quote."}'

        source = ss.SlackSource(ME_ADDR, INTERNAL, store=store)
        fake = FakeSession()
        source._session = fake
        draft = source.draft(item.id, "reply")

        check("the recipient is the channel", draft.to_addr == "#deal-acme",
              draft.to_addr)
        check("there is no subject line", draft.subject == "", draft.subject)
        check("the body came through", "Confirmed" in draft.body)
        check("it is only a draft", draft.status == "drafted")
        check("the channel is slack", draft.channel == "slack")
        check("the prompt carried the conversation",
              "confirm the uplift?" in fake.prompt)
        check("the prompt says Slack is shorter than email",
              "shorter and plainer" in fake.prompt)
        check("the prompt says not to send", "not send anything" in fake.prompt)

        class ProseSession(FakeSession):
            def ask(self, prompt, on_token=None, timeout=None):
                return "Confirmed, go ahead."

        source._session = ProseSession()
        prose = source.draft(item.id, "reply")
        check("prose is kept as the body", "Confirmed" in prose.body)
        check("and still goes to the channel", prose.to_addr == "#deal-acme")


def test_slack_allowlist_is_read_only() -> None:
    print("the Slack allowlist cannot reach a write tool")
    check("SLACK_READ has no write tool",
          not (set(cli.SLACK_READ) & set(cli.WRITE_TOOLS)))
    # The probe showed these two were denied, which silently removed mentions.
    check("the search tool is allowed",
          f"{cli.SLACK}slack_search_public_and_private" in cli.SLACK_READ)
    check("the profile tool is allowed",
          f"{cli.SLACK}slack_read_user_profile" in cli.SLACK_READ)
    check("posting is still denied",
          f"{cli.SLACK}slack_send_message" in cli.WRITE_TOOLS)
    check("reacting is still denied",
          f"{cli.SLACK}slack_add_reaction" in cli.WRITE_TOOLS)
    # An earlier prompt demanded "exactly three tool calls" and the model refused
    # the sweep: reading six DMs is one list plus six reads.
    check("the sweep prompt does not pin a call count",
          "exactly three tool calls" not in ss.SWEEP_PROMPT)
    check("it constrains which tools may be used instead",
          "Use only slack_read_user_profile" in ss.SWEEP_PROMPT)
    check("and forbids writing", "do not post, reply, react or create" in ss.SWEEP_PROMPT)
    check("and tells the model to leave markup alone",
          "do not expand" in ss.SWEEP_PROMPT)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"\n{passed} checks passed across {len(tests)} groups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
