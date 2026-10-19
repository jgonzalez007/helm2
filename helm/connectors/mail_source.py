"""MailSource — real Outlook mail through the approved connector.

Read and draft only. `send()` refuses until phase 10 wires the approval path.

One sweep fetches the inbox and sent items together, because the sent list is what
makes three other things work without a second connector call:

  * needs-reply, computed from thread structure rather than from body text
  * the waiting-on tracker, which needs to know what you sent and got no answer to
  * the voice profile the drafts are written in

Needs-reply deliberately ignores message content. It asks who sent the newest
message in the conversation, whether you were on To:, and whether you have since
replied. A question mark in the body does not make something need a reply, and a
"please confirm receipt" does not either.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Iterable

from .. import claude_cli as cli
from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread, now_iso
from ._clean import (MAX_STR, _bool, _is_external, _list_of_str, _s, any_time_to_local,
                     timezone_mismatch)
from .base import SendRefused, require_approval

# Patterns seen in the real inbox. A hardcoded list is not the final answer —
# phase 5 sends genuinely ambiguous senders to Claude — but these are unambiguous
# and were all being marked "needs your reply" without them.
BULK_HINTS = ("no-reply", "noreply", "donotreply", "notifications@", "mailer@",
              "bounce", "newsletter@", "updates@", "approvals@", "-digest@",
              "digest@", "service-now", "servicenow", "alerts@", "alert@",
              "automated@", "automation@", "jira@", "confluence@", "@eventsmail.",
              "mail.salesforce", "marketing@", "events@", "webinar@", "support@",
              "billing@", "invoices@", "health@aws", "mssecurity-")

# One prompt, two tool calls. Splitting them into two sweeps doubled the wall clock
# for no benefit — the model turnaround dominates, not the tool.
MAIL_PROMPT = """Make exactly two tool calls and no others.

1. outlook_email_search with order "newest" and limit {inbox_limit}.
2. outlook_email_search with folderName "Sent Items", order "newest", limit {sent_limit}.

Reply with ONLY a JSON object with two keys, "inbox" and "sent".

Each "inbox" entry:
  id              the message id from the tool
  conversation_id the conversation or thread id
  subject         the subject line
  from_name       the sender's display name
  from_email      the sender's email address
  to_emails       array of To: addresses
  cc_emails       array of Cc: addresses
  received        the received date-time string, copied verbatim
  timezone        the time zone string if the tool gave one, else ""
  is_read         true if the message has been read
  has_attachments true if it has attachments
  preview         the first 200 characters of the body

Each "sent" entry:
  id              the message id
  conversation_id the conversation or thread id
  subject         the subject line
  to_emails       array of To: addresses
  sent            the sent date-time string, copied verbatim
  preview         the first 200 characters of the body

Do not reformat or convert any timestamp. Use "" or [] for anything the result does
not contain. If a list is empty, return []. Do not read any individual message
resource and do not search anything else.
"""

THREAD_PROMPT = """Call outlook_email_search with query "{subject}" and limit 10, then \
read only the messages in conversation {conversation_id}.

Reply with ONLY a JSON array of the messages in that conversation, oldest first, keys:
  author   the sender's display name
  email    the sender's email address
  body     the message body as plain text, up to 1500 characters
  sent_at  the sent date-time string, copied verbatim

Do not include quoted history that repeats an earlier message in the same array.
If you cannot find the conversation, reply [].
"""

VOICE_PROMPT = """Call outlook_email_search with folderName "Sent Items", \
order "newest", limit 8, then read those messages.

Reply with ONLY a JSON array of plain-text strings: the body of each message, most
recent first, up to 600 characters each. Strip quoted history and any corporate
footer or confidentiality notice. No keys, no metadata, no prose.
"""

DRAFT_PROMPT = """Draft a reply to the last message in this thread. Do not send \
anything: this is a draft a human will read and approve.

{voice}

Thread, oldest first:
{thread}

What I want the reply to do: {intent}

Reply with ONLY a JSON object:
  subject  the reply subject line
  body     the reply body as plain text, no signature block

Write as me, in my style described above. Be specific and commit to dates only where
the thread already does. Do not invent facts, numbers or commitments. If the thread
does not contain what a good reply needs, say so in the body in one line rather than
guessing.
"""


# ── Voice profile: learned locally, no model call ───────────────────────────

_GREETING = re.compile(r"^(hi|hey|hello|good morning|good afternoon|morning|afternoon|dear)"
                       r"[ ,]*([A-Z][a-z]+)?", re.I)
_SIGNOFF = re.compile(r"^(thanks|thank you|cheers|best|regards|best regards|br|"
                      r"talk soon|appreciate it|many thanks)\b", re.I)


def build_voice_profile(sent: list[dict], sample: int = 15) -> dict:
    """Derive a writing style from your own sent mail, with plain string work.

    A model call would do this better and cost 60-120 s every refresh. The profile
    only needs to carry length, greeting and sign-off habits, which regexes read well
    enough to steer a draft.
    """
    bodies = [_s(m.get("preview"), 400) for m in sent if _s(m.get("preview"), 400)]
    if not bodies:
        return {"samples": 0}

    greetings: Counter = Counter()
    signoffs: Counter = Counter()
    word_counts: list[int] = []
    first_names = 0

    for body in bodies:
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        word_counts.append(len(body.split()))
        if lines:
            m = _GREETING.match(lines[0])
            if m:
                greetings[m.group(1).lower()] += 1
                if m.group(2):
                    first_names += 1
        for line in lines:
            m = _SIGNOFF.match(line)
            if m:
                signoffs[m.group(1).lower()] += 1
                break

    word_counts.sort()
    median = word_counts[len(word_counts) // 2] if word_counts else 0
    return {
        "samples": len(bodies),
        "greeting": greetings.most_common(1)[0][0] if greetings else "",
        "greeting_rate": round(sum(greetings.values()) / len(bodies), 2),
        "uses_first_name": first_names > len(bodies) / 3,
        "signoff": signoffs.most_common(1)[0][0] if signoffs else "",
        "median_words": median,
        "built_at": now_iso(),
    }


def describe_voice(profile: dict) -> str:
    """The profile as a sentence the draft prompt can use."""
    if not profile or not profile.get("samples"):
        return ("My style: no sent mail has been read yet, so keep the reply short, "
                "plain and direct.")
    bits = [f"My style, learned from {profile['samples']} of my own sent messages:"]
    if profile.get("greeting"):
        who = "the recipient's first name" if profile.get("uses_first_name") else "no name"
        bits.append(f'I usually open with "{profile["greeting"].capitalize()}" and {who}.')
    elif profile.get("greeting_rate", 0) < 0.3:
        bits.append("I usually skip the greeting and start with the point.")
    if profile.get("signoff"):
        bits.append(f'I usually close with "{profile["signoff"]}".')
    words = profile.get("median_words") or 0
    if words:
        bits.append(f"My replies run about {words} words. Match that, not longer.")
    return " ".join(bits)


# ── Message cleaning ────────────────────────────────────────────────────────

def _is_bulk(email: str) -> bool:
    return any(h in (email or "").lower() for h in BULK_HINTS)


_REPLY_PREFIX = re.compile(r"^((re|fw|fwd|aw|sv|vs)\s*:\s*)+", re.I)

# Outlook's own prefixes on calendar responses, which it files in Sent Items.
_MEETING_RESPONSE = re.compile(r"^(accepted|declined|tentative|canceled|cancelled"
                               r"|updated)\s*:", re.I)


def thread_key(conversation_id: str, subject: str) -> str:
    """Group messages into a thread.

    The connector returns an empty conversation_id for every message, so the
    fallback is the subject with its Re:/FW: prefixes stripped. That is how mail
    clients grouped threads before conversation ids existed, and it is structural:
    no part of the body is consulted.
    """
    if conversation_id:
        return conversation_id
    stripped = _REPLY_PREFIX.sub("", _s(subject, 200)).strip().lower()
    return f"subject:{stripped}" if stripped else ""


def _clean_message(raw, me: str, internal_domains: Iterable[str]) -> Item | None:
    if not isinstance(raw, dict):
        return None

    received = any_time_to_local(raw.get("received"))
    if not received:
        return None

    from_email = _s(raw.get("from_email"), 200).lower()
    to_emails = [e.lower() for e in _list_of_str(raw.get("to_emails"))]
    cc_emails = [e.lower() for e in _list_of_str(raw.get("cc_emails"))]
    me_lower = me.lower()

    message_id = _s(raw.get("id"), 300)
    subject = _s(raw.get("subject")) or "(no subject)"
    if not message_id:
        message_id = f"mail-{abs(hash((subject, received, from_email))):x}"

    return Item(
        id=message_id,
        source="mail",
        kind="email",
        subject=subject,
        sender=_s(raw.get("from_name"), 120) or from_email or "unknown sender",
        sender_addr=from_email,
        sender_external=_is_external(from_email, internal_domains),
        snippet=_s(raw.get("preview"), 300),
        thread_id=thread_key(_s(raw.get("conversation_id"), 300), subject)
        or message_id,
        received_at=received,
        # On To: is a structural signal the flag rules are allowed to use. Being on
        # Cc only, or on neither, is not the same as being asked.
        to_me_direct=me_lower in to_emails,
        cc_me=me_lower in cc_emails,
        state="needs_me",
    )


def _clean_sent(raw, me: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    sent_at = any_time_to_local(raw.get("sent"))
    if not sent_at:
        return None
    subject = _s(raw.get("subject")) or "(no subject)"
    return {
        "id": _s(raw.get("id"), 300),
        "conversation_id": thread_key(_s(raw.get("conversation_id"), 300), subject),
        "subject": subject,
        "to_emails": [e.lower() for e in _list_of_str(raw.get("to_emails"))],
        "sent_at": sent_at,
        "preview": _s(raw.get("preview"), 400),
    }


def needs_reply(item: Item, replied_conversations: set[str],
                profiles: dict[str, dict] | None = None) -> tuple[bool, str]:
    """Structural only. Returns (needs_reply, the reason to show).

    Message content is not consulted. A thread needs a reply because of who sent the
    last message and whether you were asked, not because of any phrase in it.

    What HELM has already learned about the sender is checked first: a click or a
    stage-2 verdict is more reliable than guessing from the shape of an address, and
    costs nothing to apply.
    """
    addr = (item.sender_addr or "").lower()
    profile = (profiles or {}).get(addr) or {}
    if profile.get("pinned"):
        if not profile.get("expects_reply", True):
            return False, profile.get("reason") or "you told HELM this sender needs no reply"
        return True, profile.get("reason") or "you marked this sender as one you answer"
    if profile.get("is_automated") or not profile.get("expects_reply", True):
        return False, profile.get("reason") or "a system or digest sender"

    if addr.startswith(("postmaster@", "mailer-daemon@")):
        return False, "bounce notification"
    if _is_bulk(item.sender_addr):
        return False, "bulk or no-reply address"
    if not item.to_me_direct:
        # Measured live: most of this inbox is addressed to a shared mailbox or
        # distribution list, not to the user on Cc. Saying "Cc" would be wrong.
        return False, ("addressed to a list or shared mailbox, not to you directly"
                       if not item.cc_me else "you are on Cc, not To")
    if item.thread_id in replied_conversations:
        return False, "you have already replied in this thread"
    return True, "newest message is theirs and you are on To:"


def find_waiting(sent: list[dict], inbox: list[Item], me: str,
                 nudge_days: int = 3, now: datetime | None = None) -> list[Item]:
    """Things you sent that nobody answered. Feeds the waiting-on tracker."""
    now = now or datetime.now().astimezone()
    inbound_newest: dict[str, str] = {}
    for item in inbox:
        prev = inbound_newest.get(item.thread_id, "")
        if item.received_at > prev:
            inbound_newest[item.thread_id] = item.received_at

    out: list[Item] = []
    seen: set[str] = set()
    for msg in sent:
        convo = msg["conversation_id"]
        if not convo or convo in seen:
            continue
        # Outlook files RSVPs in Sent Items. An RSVP is not something you are
        # waiting on a reply to, and the live run surfaced four of them.
        if _MEETING_RESPONSE.match(msg["subject"]):
            continue
        # A message whose only recipient is you is a note to self, not a question
        # anyone owes you an answer to.
        recipients = [r for r in msg["to_emails"] if r != me.lower()]
        if not recipients:
            continue
        reply = inbound_newest.get(convo, "")
        if reply and reply > msg["sent_at"]:
            continue                      # they answered
        try:
            age = (now - datetime.fromisoformat(msg["sent_at"])).days
        except ValueError:
            continue
        if age < nudge_days:
            continue                      # too soon to call it waiting
        seen.add(convo)
        out.append(Item(
            id=f"waiting-{msg['id'] or convo}",
            source="mail",
            kind="email",
            subject=msg["subject"],
            sender="You",
            sender_addr=me,
            snippet=msg["preview"],
            thread_id=convo,
            received_at=msg["sent_at"],
            sent_by_me_at=msg["sent_at"],
            state="waiting",
            to_me_direct=True,
        ))
    return out


class MailSource:
    """Read and draft. Sending is refused until phase 10."""

    name = "mail"
    # A mail thread costs its own connector call, so the sweep must not pull one
    # per message. The drawer fetches it when you open the item.
    prefetch_threads = False

    # Measured sweeps: 197 s, 232 s, 242 s. The shared 240 s default was marginal and
    # the third run timed out, reporting an empty inbox. Mail gets its own ceiling
    # and a smaller inbox page: 15 recent messages is more than a triage pass needs.
    SWEEP_TIMEOUT = 420

    def __init__(self, me: str, internal_domains: Iterable[str],
                 availability: cli.Availability | None = None,
                 store=None, inbox_limit: int = 15, sent_limit: int = 12) -> None:
        self.me = me
        self.internal_domains = list(internal_domains)
        self.availability = availability
        self.inbox_limit = inbox_limit
        self.sent_limit = sent_limit
        self._store = store            # used only to cache the voice profile
        self._status = SourceStatus(source=self.name, health="stale")
        self._bus: EventBus | None = None
        self._session: cli.Session | None = None
        self._replied: set[str] = set()
        self._reply_reasons: dict[str, str] = {}
        self._waiting: list[Item] = []
        self.voice: dict = {}

    # ── Connector protocol ──────────────────────────────────────────────────

    def status(self) -> SourceStatus:
        return self._status

    def list_new(self, since: str = "") -> list[Item]:
        av = self.availability
        if av is not None and not av.microsoft_365:
            self._status = SourceStatus(source=self.name, health="unavailable",
                                        last_error=av.reason("Microsoft 365"),
                                        last_ok_at=self._status.last_ok_at)
            return []

        prompt = MAIL_PROMPT.format(inbox_limit=self.inbox_limit, sent_limit=self.sent_limit)
        try:
            reply, elapsed = cli.run_once(prompt, cli.MAIL_READ,
                                          timeout=self.SWEEP_TIMEOUT)
        except cli.ClaudeUnavailable as e:
            self._status = SourceStatus(source=self.name, health="reconnecting",
                                        last_error=str(e)[:300],
                                        last_ok_at=self._status.last_ok_at)
            return []

        parsed = cli.extract_json(reply)
        if not isinstance(parsed, dict):
            self._status = SourceStatus(
                source=self.name, health="reconnecting",
                last_error=f"could not read the reply as JSON: {reply[:160]}",
                last_ok_at=self._status.last_ok_at, measured_latency_ms=elapsed)
            return []

        # A dict without an "inbox" list is not an empty inbox, it is a reply we did
        # not understand. Treating the two the same reported a successful sweep with
        # zero rows on the first live run — the exact failure that makes a quiet
        # panel untrustworthy.
        if not isinstance(parsed.get("inbox"), list):
            self._status = SourceStatus(
                source=self.name, health="reconnecting",
                last_error=f"reply had no inbox list: {reply[:200]}",
                last_ok_at=self._status.last_ok_at, measured_latency_ms=elapsed)
            return []

        raw_inbox = parsed["inbox"]
        raw_sent = parsed.get("sent") if isinstance(parsed.get("sent"), list) else []

        inbox = [m for m in (_clean_message(r, self.me, self.internal_domains)
                             for r in raw_inbox) if m is not None]
        sent = [m for m in (_clean_sent(r, self.me) for r in raw_sent) if m is not None]

        # Anything you have replied to in the sent list is not awaiting you.
        self._replied = {m["conversation_id"] for m in sent if m["conversation_id"]}
        self._reply_reasons = {}
        profiles = self._store.sender_profiles() if self._store is not None else {}
        for item in inbox:
            wanted, reason = needs_reply(item, self._replied, profiles)
            self._reply_reasons[item.id] = reason
            if not wanted:
                item.state = "done" if item.thread_id in self._replied else "working"

        self._waiting = find_waiting(sent, inbox, self.me)
        self._refresh_voice(sent)

        tz_warning = next((w for w in (timezone_mismatch(_s(r.get("timezone"), 64))
                                       for r in raw_inbox if isinstance(r, dict)) if w), "")
        self._status = SourceStatus(source=self.name, health="live", last_ok_at=now_iso(),
                                    last_error=tz_warning, measured_latency_ms=elapsed,
                                    count=len([i for i in inbox if i.state == "needs_me"]))
        return inbox + self._waiting

    def reply_reason(self, item_id: str) -> str:
        return self._reply_reasons.get(item_id, "")

    def _refresh_voice(self, sent: list[dict]) -> None:
        """Build from the sweep's previews when they carry text.

        Measured live: this connector returns an empty preview for Sent Items, so
        this usually no-ops and `refresh_voice()` does the real work.
        """
        profile = build_voice_profile(sent)
        if not profile.get("samples"):
            return
        self._save_voice(profile)

    def _save_voice(self, profile: dict) -> None:
        self.voice = profile
        if self._store is not None:
            self._store.set_setting("voice_profile", profile)

    def refresh_voice(self, force: bool = False, max_age_hours: int = 24) -> dict:
        """Read a few of your sent messages properly and rebuild the profile.

        Its own connector call, because the sweep's previews come back empty. How you
        write does not change hourly, so this runs at most once a day unless asked.
        """
        stored = (self._store.get_setting("voice_profile", {})
                  if self._store is not None else {})
        if not force and stored.get("built_at"):
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(stored["built_at"])
                if age < timedelta(hours=max_age_hours):
                    self.voice = stored
                    return stored
            except ValueError:
                pass

        try:
            reply, _ = cli.run_once(VOICE_PROMPT, cli.MAIL_READ)
        except cli.ClaudeUnavailable as e:
            return {"samples": 0, "error": str(e)[:200]}

        parsed = cli.extract_json(reply)
        if not isinstance(parsed, list):
            return {"samples": 0, "error": f"could not read the reply: {reply[:160]}"}

        bodies = [{"preview": _s(b, 600)} for b in parsed if isinstance(b, str) and b.strip()]
        profile = build_voice_profile(bodies)
        if profile.get("samples"):
            self._save_voice(profile)
        return profile

    def get_thread(self, item_id: str) -> Thread:
        """Fetched on demand only — never during a sweep.

        A thread costs its own connector call. Pulling one per message would turn a
        single 2-minute sweep into half an hour.
        """
        item = self._store.item(item_id) if self._store is not None else None
        if item is None:
            return Thread(item_id=item_id, messages=[])

        prompt = THREAD_PROMPT.format(subject=item.subject.replace('"', "'")[:120],
                                      conversation_id=item.thread_id)
        try:
            reply, _ = cli.run_once(prompt, cli.MAIL_READ)
        except cli.ClaudeUnavailable:
            return Thread(item_id=item_id, messages=[])

        parsed = cli.extract_json(reply)
        if not isinstance(parsed, list):
            return Thread(item_id=item_id, messages=[])

        me_lower = self.me.lower()
        messages = []
        for raw in parsed[:20]:
            if not isinstance(raw, dict):
                continue
            email = _s(raw.get("email"), 200).lower()
            messages.append({
                "author": _s(raw.get("author"), 120) or email or "unknown",
                "body": _s(raw.get("body"), 1500),
                "sent_at": any_time_to_local(raw.get("sent_at")) or _s(raw.get("sent_at"), 40),
                "is_me": email == me_lower,
            })
        return Thread(item_id=item_id, messages=messages)

    # ── Drafting: never sends ───────────────────────────────────────────────

    def session(self) -> cli.Session:
        if self._session is None:
            self._session = cli.Session(cli.MAIL_READ)
        return self._session

    def draft(self, item_id: str, intent: str, on_token=None) -> Draft:
        item = self._store.item(item_id) if self._store is not None else None
        if item is None:
            raise NotImplementedError("no such message")

        thread = self._store.thread(item_id) if self._store is not None else []
        if not thread:
            thread = [{"author": item.sender, "body": item.snippet,
                       "sent_at": item.received_at, "is_me": False}]
        rendered = "\n\n".join(
            f"[{'me' if m['is_me'] else m['author']}, {m['sent_at'][:16]}]\n{m['body'][:1200]}"
            for m in thread[-6:])

        voice = self.voice or (self._store.get_setting("voice_profile", {})
                               if self._store is not None else {})
        prompt = DRAFT_PROMPT.format(voice=describe_voice(voice), thread=rendered,
                                     intent=_s(intent, 300) or "reply appropriately")

        reply = self.session().ask(prompt, on_token=on_token)
        parsed = cli.extract_json(reply)
        if isinstance(parsed, dict):
            subject = _s(parsed.get("subject"), 300)
            body = _s(parsed.get("body"), 4000)
        else:
            # The model answered in prose. Use it as the body rather than losing it;
            # the human is going to read this before anything happens to it.
            subject, body = "", _s(reply, 4000)

        if not subject:
            subject = item.subject if item.subject.lower().startswith("re:") \
                else f"Re: {item.subject}"

        return Draft(
            id=f"draft-{uuid.uuid4().hex[:8]}",
            item_id=item_id,
            channel="mail",
            to_addr=item.sender_addr,
            subject=subject[:MAX_STR],
            body=body,
            created_at=now_iso(),
        )

    def send(self, draft_id: str, approval: ApprovalToken) -> dict:
        require_approval(draft_id, approval)
        raise SendRefused("mail sending is wired in phase 10")

    def subscribe(self, bus: EventBus) -> None:
        self._bus = bus

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
