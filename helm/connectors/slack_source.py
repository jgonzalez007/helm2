"""SlackSource — real Slack through the approved connector.

Read and draft only. `send()` refuses until phase 10.

One sweep, three tool calls: who I am, my DMs and group DMs, and messages that
mention me. Measured separately at 97 s, 73 s, 68 s and 92 s, so the sweep gets its
own generous ceiling rather than the shared default.

Two things the probe forced into the design:

* `<!here>` is not a mention of you. The only real mention in a three-day search was
  a `<!here> Team,` broadcast. Treating a broadcast as a direct ping would put every
  channel announcement at the top of the focus queue, so `direct_ping` requires your
  own user id in the text.
* Slack has no email addresses on messages, but the observed handles are exactly the
  email local part (`jose.gonzalez`, `sree.kancharla`). The handle plus the primary
  internal domain is used as the address so one VIP list works across both sources.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Iterable

from .. import claude_cli as cli
from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread, now_iso
from ._clean import MAX_STR, _bool, _list_of_str, _s, any_time_to_local
from .base import SendRefused, require_approval

# Do not pin a call count. An earlier version demanded "exactly three tool calls" and
# the model refused the whole sweep, correctly: reading six DMs is one list plus six
# reads. Constrain which tools may be used, not how many times.
SWEEP_PROMPT = """Gather three things, using only the tools named below.

1. My own Slack user id and handle (slack_read_user_profile).
2. My direct messages and group DMs: list them with slack_list_user_channels using
   types "im,mpim" and limit {dm_limit}, then read the most recent {per_dm} messages
   from each of the {dm_read} most recently active of those conversations.
3. Messages from the last {days} days that mention me, found with a Slack search.

Use only slack_read_user_profile, slack_list_user_channels, slack_read_channel,
slack_read_thread and the Slack search tools. Use as many reads as step 2 needs.
Do not use any other tool, and do not post, reply, react or create anything.

Reply with ONLY a JSON object with three keys: "me", "dms", "mentions".

"me" is an object: id, handle, real_name.

Each "dms" entry:
  id            a stable id for the message, such as "<channel_id>:<timestamp>"
  channel       the conversation's display name
  channel_id    the conversation id
  channel_type  "im" or "mpim"
  author        the sender's display name
  author_handle the sender's Slack handle, without the @
  author_id     the sender's user id
  text          the message text, up to 400 characters, exactly as Slack returned it
  sent_at       the message time as ISO 8601 with a UTC offset, such as
                "2026-10-15T23:30:56-05:00" — never a zone abbreviation
  is_me         true if I sent it

Each "mentions" entry: the same keys, plus
  permalink     a link to the message, or ""
and channel_type "channel".

A Slack timestamp is a real instant, so converting it to ISO 8601 with an offset is
safe and is what this needs. Leave the text's Slack markup
alone: do not expand <@U123>, <!here> or <http://x|y>. Use "" or [] for anything the
result does not contain. If a list is empty, return [].
"""

DRAFT_PROMPT = """Draft a Slack reply to the last message in this conversation. Do \
not send anything: this is a draft a human will read and approve.

{voice}

Slack replies are shorter and plainer than email. No greeting, no sign-off, no
subject line. One or two sentences unless the question genuinely needs more.

Conversation, oldest first:
{thread}

What I want the reply to do: {intent}

Reply with ONLY a JSON object with one key, "body": the message text.
Do not invent facts, numbers or commitments. If the conversation does not contain
what a good reply needs, say so in one line rather than guessing.
"""

# Slack's own markup for addressing a room. None of these is a mention of you.
BROADCASTS = ("<!here>", "<!channel>", "<!everyone>")

_USER_REF = re.compile(r"<@([UW][A-Z0-9]+)(\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#(C[A-Z0-9]+)(\|([^>]*))?>")
_LINK = re.compile(r"<(https?://[^|>]+)(\|([^>]*))?>")
_SPECIAL = re.compile(r"<!(\w+)(\|[^>]*)?>")


def to_plain(text: str, names: dict[str, str] | None = None) -> str:
    """Slack markup to something a person can read in a list row.

    Kept out of the sweep: the raw text is stored so a later phase can re-render it,
    and the UI never has to trust a pre-flattened string.
    """
    names = names or {}
    out = _s(text, 1000)
    out = _USER_REF.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), out)
    out = _CHANNEL_REF.sub(lambda m: "#" + (m.group(3) or m.group(1)), out)
    out = _LINK.sub(lambda m: m.group(3) or m.group(1), out)
    out = _SPECIAL.sub(lambda m: "@" + m.group(1), out)
    return re.sub(r"\s+", " ", out).strip()


def mentions_me(text: str, my_id: str) -> bool:
    """A direct ping, not a room announcement.

    `<!here>` reached one real message in a three-day search. Counting it as a
    mention would put every channel announcement at the top of the focus queue.
    """
    if not my_id:
        return False
    return any(m.group(1) == my_id for m in _USER_REF.finditer(_s(text, 2000)))


def is_broadcast(text: str) -> bool:
    low = _s(text, 2000).lower()
    return any(tag in low for tag in BROADCASTS)


def handle_to_address(handle: str, internal_domains: Iterable[str]) -> str:
    """shortcut: Slack carries no email, and every handle observed here is exactly
    the email local part, so one VIP list can cover both sources. Upgrade to a real
    profile lookup if a workspace ever uses handles that differ from addresses.
    """
    handle = _s(handle, 100).lstrip("@").lower()
    domains = [d for d in internal_domains if d]
    if not handle or not domains:
        return ""
    return f"{handle}@{domains[0].lower()}"


def _clean_message(raw, me_id: str, internal_domains: Iterable[str],
                   my_address: str = "") -> Item | None:
    if not isinstance(raw, dict):
        return None

    sent_at = any_time_to_local(raw.get("sent_at"))
    if not sent_at:
        return None

    channel_id = _s(raw.get("channel_id"), 100)
    message_id = _s(raw.get("id"), 200) or f"{channel_id}:{sent_at}"
    text = _s(raw.get("text"), 1000)
    channel = _s(raw.get("channel"), 160) or channel_id or "Slack"
    channel_type = _s(raw.get("channel_type"), 20).lower()
    handle = _s(raw.get("author_handle"), 100)

    direct_ping = mentions_me(text, me_id)
    dm = channel_type in ("im", "mpim")
    group = channel_type == "mpim"
    kind = "dm" if dm else ("mention" if direct_ping else "thread")

    # The connector states whether a message is yours. Use that rather than
    # comparing display names — Slack reports "Jose Gonzalez" where the handle is
    # "jose.gonzalez", so a name comparison silently failed.
    from_me = _bool(raw.get("is_me"))
    sender_addr = my_address if from_me else handle_to_address(handle, internal_domains)

    return Item(
        id=f"slack-{message_id}",
        source="slack",
        kind=kind,
        subject=channel if dm else f"#{channel.lstrip('#')}",
        sender=_s(raw.get("author"), 120) or handle or "unknown",
        sender_addr=sender_addr,
        snippet=to_plain(text),
        thread_id=channel_id or channel,
        received_at=sent_at,
        # A DM is addressed to you. A channel message is only addressed to you if it
        # names you; `<!here>` is addressed to the room.
        to_me_direct=dm or direct_ping,
        group_chat=group,
        state="needs_me",
    )


def _age_days(stamp: str, now: datetime | None = None) -> float | None:
    try:
        when = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    now = now or datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()
    return (now - when).total_seconds() / 86400


def by_conversation(items: list[Item]) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {}
    for item in items:
        out.setdefault(item.thread_id, []).append(item)
    for msgs in out.values():
        msgs.sort(key=lambda m: m.received_at)
    return out


def newest_per_conversation(items: list[Item]) -> dict[str, Item]:
    newest: dict[str, Item] = {}
    for item in items:
        known = newest.get(item.thread_id)
        if known is None or item.received_at > known.received_at:
            newest[item.thread_id] = item
    return newest


# A conversation nobody has touched in this long is not awaiting your reply. The
# first live sweep queued a group DM whose newest message was six months old.
STALE_DAYS = 14


def needs_reply(item: Item, newest: dict[str, Item], my_address: str,
                profiles: dict[str, dict] | None = None,
                conversation: list[Item] | None = None,
                now: datetime | None = None) -> tuple[bool, str]:
    """Structural only: who spoke last, were you addressed, and how long ago.

    The message text is read for exactly one thing — whether it contains your user
    id — which is Slack's addressing mechanism, not a judgement about content.
    """
    profile = (profiles or {}).get((item.sender_addr or "").lower()) or {}
    if profile.get("pinned") and not profile.get("expects_reply", True):
        return False, profile.get("reason") or "you told HELM this sender needs no reply"
    if profile.get("is_automated"):
        return False, profile.get("reason") or "a bot or app, not a person"

    last = newest.get(item.thread_id)
    if last is not None and last.id != item.id:
        return False, "a later message in this conversation"
    if last is not None and last.sender_addr             and last.sender_addr.lower() == (my_address or "~none~").lower():
        return False, "you spoke last here"

    age_days = _age_days(item.received_at, now)
    if age_days is not None and age_days > STALE_DAYS:
        return False, f"nothing new here in {int(age_days)} days"

    if item.kind == "dm" and item.group_chat and not mentions_me(item.snippet, ""):
        # In a room with several people, somebody else's message is not necessarily
        # to you. The first live sweep surfaced two group DMs where two other people
        # were talking to each other. Require that you are part of the exchange.
        spoke = [m for m in (conversation or [])
                 if (m.sender_addr or "").lower() == (my_address or "~").lower()]
        if not spoke:
            return False, "a group conversation you have not joined"
        if spoke and item.received_at <= max(m.received_at for m in spoke):
            return False, "you spoke last here"
        return True, "the group replied after you did"

    if item.kind == "dm":
        return True, "newest message in a direct message, and it is not yours"
    if item.kind == "mention":
        return True, "you were named directly in this channel"
    return False, "a channel message that does not name you"


class SlackSource:
    """Read and draft. Sending is refused until phase 10."""

    name = "slack"
    prefetch_threads = True        # the sweep already returns the recent messages

    # Probed separately at 97 s, 73 s, 68 s and 92 s. Three calls in one prompt needs
    # room, and the shared 240 s default has already caused one silent empty sweep.
    SWEEP_TIMEOUT = 480

    def __init__(self, me: str, internal_domains: Iterable[str],
                 availability: cli.Availability | None = None, store=None,
                 dm_limit: int = 20, dm_read: int = 6, per_dm: int = 4,
                 days: int = 3) -> None:
        self.me = me
        self.internal_domains = list(internal_domains)
        self.availability = availability
        self._store = store
        self.dm_limit = dm_limit
        self.dm_read = dm_read
        self.per_dm = per_dm
        self.days = days
        self.my_id = ""
        self.my_handle = ""
        self._status = SourceStatus(source=self.name, health="stale")
        self._bus: EventBus | None = None
        self._session: cli.Session | None = None
        self._threads: dict[str, list[dict]] = {}
        self._reply_reasons: dict[str, str] = {}
        self._broadcasts: set[str] = set()

    # ── Connector protocol ──────────────────────────────────────────────────

    def status(self) -> SourceStatus:
        return self._status

    def list_new(self, since: str = "") -> list[Item]:
        av = self.availability
        if av is not None and not av.slack:
            self._status = SourceStatus(source=self.name, health="unavailable",
                                        last_error=av.reason("Slack"),
                                        last_ok_at=self._status.last_ok_at)
            return []

        prompt = SWEEP_PROMPT.format(dm_limit=self.dm_limit, dm_read=self.dm_read,
                                     per_dm=self.per_dm, days=self.days)
        try:
            reply, elapsed = cli.run_once(prompt, cli.SLACK_READ,
                                          timeout=self.SWEEP_TIMEOUT)
        except cli.ClaudeUnavailable as e:
            self._status = SourceStatus(source=self.name, health="reconnecting",
                                        last_error=str(e)[:300],
                                        last_ok_at=self._status.last_ok_at)
            return []

        parsed = cli.extract_json(reply)
        # A dict without a "dms" list is a reply we did not understand, not a quiet
        # Slack. Phase 3 learned that lesson the expensive way.
        if not isinstance(parsed, dict) or not isinstance(parsed.get("dms"), list):
            self._status = SourceStatus(
                source=self.name, health="reconnecting",
                last_error=f"reply had no dms list: {reply[:200]}",
                last_ok_at=self._status.last_ok_at, measured_latency_ms=elapsed)
            return []

        who = parsed.get("me") if isinstance(parsed.get("me"), dict) else {}
        self.my_id = _s(who.get("id"), 40)
        self.my_handle = _s(who.get("handle"), 100).lstrip("@")

        raw_dms = parsed["dms"]
        raw_mentions = parsed.get("mentions") if isinstance(parsed.get("mentions"),
                                                            list) else []

        messages: list[Item] = []
        for raw in raw_dms:
            item = _clean_message(raw, self.my_id, self.internal_domains, self.me.lower())
            if item is not None:
                messages.append(item)
        for raw in raw_mentions:
            item = _clean_message(raw, self.my_id, self.internal_domains, self.me.lower())
            if item is None:
                continue
            if is_broadcast(_s(raw.get("text"), 1000)) and item.kind != "mention":
                self._broadcasts.add(item.id)
            messages.append(item)

        # The recent messages are the thread: store them so the drawer needs no call.
        self._threads = {}
        for item in messages:
            self._threads.setdefault(item.thread_id, []).append({
                "author": item.sender, "body": item.snippet,
                "sent_at": item.received_at,
                "is_me": (item.sender_addr or "").lower() == self.me.lower(),
            })
        for msgs in self._threads.values():
            msgs.sort(key=lambda m: m["sent_at"])

        newest = newest_per_conversation(messages)
        grouped = by_conversation(messages)
        profiles = self._store.sender_profiles() if self._store is not None else {}
        surfaced: list[Item] = []
        self._reply_reasons = {}
        for item in messages:
            wanted, reason = needs_reply(item, newest, self.me.lower(), profiles,
                                         grouped.get(item.thread_id))
            self._reply_reasons[item.id] = reason
            if not wanted:
                # Only the newest message in a conversation is a row; the rest are
                # its thread, and a channel message that does not name you is noise.
                if newest.get(item.thread_id) is not item:
                    continue
                item.state = "done"
            surfaced.append(item)

        self._status = SourceStatus(source=self.name, health="live", last_ok_at=now_iso(),
                                    measured_latency_ms=elapsed,
                                    count=len([i for i in surfaced
                                               if i.state == "needs_me"]))
        return surfaced

    def reply_reason(self, item_id: str) -> str:
        return self._reply_reasons.get(item_id, "")

    def get_thread(self, item_id: str) -> Thread:
        item = self._store.item(item_id) if self._store is not None else None
        if item is None:
            return Thread(item_id=item_id, messages=[])
        return Thread(item_id=item_id, messages=self._threads.get(item.thread_id, []))

    # ── Drafting: never sends ───────────────────────────────────────────────

    def session(self) -> cli.Session:
        if self._session is None:
            self._session = cli.Session(cli.SLACK_READ)
        return self._session

    def draft(self, item_id: str, intent: str, on_token=None) -> Draft:
        item = self._store.item(item_id) if self._store is not None else None
        if item is None:
            raise NotImplementedError("no such message")

        thread = (self._store.thread(item_id) if self._store is not None else []) \
            or self._threads.get(item.thread_id, []) \
            or [{"author": item.sender, "body": item.snippet,
                 "sent_at": item.received_at, "is_me": False}]
        rendered = "\n".join(
            f"[{'me' if m['is_me'] else m['author']}] {m['body'][:600]}"
            for m in thread[-8:])

        from .mail_source import describe_voice
        voice = (self._store.get_setting("voice_profile", {})
                 if self._store is not None else {})
        prompt = DRAFT_PROMPT.format(voice=describe_voice(voice), thread=rendered,
                                     intent=_s(intent, 300) or "reply appropriately")

        reply = self.session().ask(prompt, on_token=on_token)
        parsed = cli.extract_json(reply)
        body = _s(parsed.get("body"), 3000) if isinstance(parsed, dict) \
            else _s(reply, 3000)

        return Draft(
            id=f"draft-{uuid.uuid4().hex[:8]}",
            item_id=item_id,
            channel="slack",
            # The conversation it came from, never an address derived from a handle.
            to_addr=item.subject,
            subject="",
            body=body,
            created_at=now_iso(),
        )

    def send(self, draft_id: str, approval: ApprovalToken) -> dict:
        require_approval(draft_id, approval)
        raise SendRefused("posting to Slack is wired in phase 10")

    def subscribe(self, bus: EventBus) -> None:
        self._bus = bus

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
