"""DemoSource — fixtures only, no network, no accounts, no credentials.

The fixture set deliberately covers all four flag quadrants, plus a
phishing-style "urgent" message from an external sender whose urgency language
must have no effect on its flags. That last one is the visible proof of the
rule in CLAUDE.md, so do not remove it.

Times are relative to launch so the countdowns and deadlines are real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread, now_iso
from .base import require_approval

ME = "jose.gonzalez@sailpoint.com"

# Demo mode seeds these so the first run has meaningful flags before the user has
# typed a single name. Real runs start with empty lists — a VIP list is the user's
# to own, and guessing one would be worse than showing none.
DEMO_SETTINGS = {
    "managers": ["scott.chen@sailpoint.com"],
    "vips": ["heidi.stark@sailpoint.com", "bryan.kelley@sailpoint.com"],
    "internal_domains": ["sailpoint.com"],
}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _fixtures(now: datetime) -> tuple[list[Item], dict[str, list[dict]]]:
    mins = lambda n: now + timedelta(minutes=n)   # noqa: E731
    hrs = lambda n: now + timedelta(hours=n)      # noqa: E731
    days = lambda n: now + timedelta(days=n)      # noqa: E731

    items: list[Item] = [
        # ── important + urgent ─────────────────────────────────────────────
        Item(
            id="demo-mail-1", source="mail", kind="email",
            subject="Q4 forecast sign-off needed by Thursday",
            sender="Scott Chen", sender_addr="scott.chen@sailpoint.com",
            snippet="Need your numbers before the Thursday board pack goes out.",
            thread_id="t-forecast", received_at=_iso(hrs(-2)),
            # You committed to Wednesday in the thread; Thursday is Scott's backstop.
            deadline_at=_iso(days(2).replace(hour=17, minute=0)),
            to_me_direct=True,
        ),
        Item(
            id="demo-slack-1", source="slack", kind="mention",
            subject="#deal-acme — @you pricing question",
            sender="Heidi Stark", sender_addr="heidi.stark@sailpoint.com",
            snippet="@jose can you confirm the ELA uplift before I send the quote?",
            thread_id="#deal-acme", received_at=_iso(hrs(-4)),
            deadline_at=_iso(hrs(6)), to_me_direct=True,
        ),

        # ── important, not urgent → morning brief + suggested time block ────
        Item(
            id="demo-mail-2", source="mail", kind="email",
            subject="FY27 territory planning — your input",
            sender="Bryan Kelley", sender_addr="bryan.kelley@sailpoint.com",
            snippet="No rush, but I'd like your view on the west coast split.",
            thread_id="t-territory", received_at=_iso(days(-1)),
            to_me_direct=True,
        ),

        # ── urgent, not important → quick reply / delegate ──────────────────
        Item(
            id="demo-mail-3", source="mail", kind="email",
            subject="Re: PO 88213 overdue — respond by tomorrow",
            sender="AP Invoices", sender_addr="ap-invoices@sailpoint.com",
            snippet="This purchase order is past its approval window.",
            thread_id="t-po", received_at=_iso(days(-1)),
            deadline_at=_iso(hrs(20)), to_me_direct=True,
        ),

        # ── neither → batched digest ────────────────────────────────────────
        Item(
            id="demo-mail-4", source="mail", kind="email",
            subject="Weekly engineering newsletter",
            sender="Eng Comms", sender_addr="no-reply@sailpoint.com",
            snippet="What shipped this week across the platform teams.",
            thread_id="t-news", received_at=_iso(hrs(-9)), to_me_direct=False,
        ),
        Item(
            id="demo-slack-2", source="slack", kind="thread",
            subject="#random — lunch thread",
            sender="Atlee Chalwell", sender_addr="atlee.chalwell@sailpoint.com",
            snippet="anyone up for the taco place at 12?",
            thread_id="#random", received_at=_iso(mins(-35)), to_me_direct=False,
        ),

        # ── the phishing fixture: loud urgency, external sender ─────────────
        # Its flags must come out low/none. The warning is display-only.
        Item(
            id="demo-mail-5", source="mail", kind="email",
            subject="URGENT: action required — verify your account within 24 hours",
            sender="IT Helpdesk", sender_addr="it-support@helpdesk-verify.net",
            sender_external=True,
            snippet=("Your mailbox will be suspended immediately. "
                     "Click here to verify your account. Do not ignore this notice."),
            thread_id="t-phish", received_at=_iso(mins(-50)), to_me_direct=True,
        ),

        # ── waiting on others ───────────────────────────────────────────────
        Item(
            id="demo-mail-6", source="mail", kind="email",
            subject="Re: SOW redlines for Northwind",
            sender="You", sender_addr=ME,
            snippet="Sending the redlines over — let me know if legal needs anything else.",
            thread_id="t-sow", received_at=_iso(days(-5)),
            sent_by_me_at=_iso(days(-5)), state="waiting", to_me_direct=True,
        ),

        # ── calendar ────────────────────────────────────────────────────────
        Item(
            id="demo-cal-1", source="calendar", kind="event",
            subject="Jose <> Scott || Weekly 1:1",
            sender="Scott Chen", sender_addr="scott.chen@sailpoint.com",
            snippet="Standing 1:1.",
            starts_at=_iso(mins(46)), ends_at=_iso(mins(76)),
            attendees=["Scott Chen"], has_agenda=False, rsvp_pending=False,
            join_url="https://teams.microsoft.com/l/meetup-join/demo-1",
            organizer_is_me=False,
        ),
        Item(
            id="demo-cal-2", source="calendar", kind="event",
            subject="Northwind — technical deep dive",
            sender="Priya Raman", sender_addr="priya.raman@northwind-example.com",
            snippet="Security architecture questions from their team.",
            starts_at=_iso(hrs(4)), ends_at=_iso(hrs(5)),
            attendees=["Priya Raman", "Scott Chen", "You"],
            external_attendees=True, presenter_is_me=True, rsvp_pending=True,
            has_agenda=False,
            join_url="https://teams.microsoft.com/l/meetup-join/demo-2",
        ),
        Item(
            id="demo-cal-3", source="calendar", kind="event",
            subject="Teams/SharePoint task discussion",
            sender="Aidan Veseling", sender_addr="aidan.veseling@sailpoint.com",
            starts_at=_iso(hrs(4).replace(minute=30)), ends_at=_iso(hrs(5).replace(minute=30)),
            attendees=["Aidan Veseling"], conflicts_with=["demo-cal-2"],
            join_url="https://teams.microsoft.com/l/meetup-join/demo-3",
        ),
    ]

    threads: dict[str, list[dict]] = {
        "demo-mail-1": [
            {"author": "Scott Chen", "body": "Jose — board pack goes out Thursday morning. "
                                             "I need the Q4 forecast numbers signed off before then.",
             "sent_at": _iso(hrs(-2)), "is_me": False},
            {"author": "You", "body": "Looking at it now, will confirm by Wednesday.",
             "sent_at": _iso(hrs(-1)), "is_me": True},
            {"author": "Scott Chen", "body": "Thanks. Flag early if the EMEA number moves.",
             "sent_at": _iso(hrs(-1)), "is_me": False},
        ],
        "demo-slack-1": [
            {"author": "Heidi Stark",
             "body": "@jose can you confirm the ELA uplift before I send the quote?",
             "sent_at": _iso(hrs(-4)), "is_me": False},
        ],
        "demo-cal-2": [
            {"author": "Priya Raman",
             "body": "Our security team has questions on tenant isolation and key handling.",
             "sent_at": _iso(days(-2)), "is_me": False},
        ],
    }
    return items, threads


# One draft per fixture, so an approval card always shows a reply that belongs to
# the item it was raised from. (subject, recipient, body)
DRAFTS = {
    "demo-mail-1": (
        "Re: Q4 forecast sign-off needed by Thursday",
        "scott.chen@sailpoint.com",
        "Scott — numbers land Wednesday morning. EMEA is the only line still "
        "moving; I'll flag it separately if it shifts more than a point."),
    "demo-slack-1": (
        "Re: #deal-acme pricing question",
        "#deal-acme",
        "Confirmed on the ELA uplift — go ahead and send the quote. Shout if "
        "legal wants the redlines attached."),
    "demo-mail-2": (
        "Re: FY27 territory planning — your input",
        "bryan.kelley@sailpoint.com",
        "Bryan — I'd keep the west coast split as it stands through Q1 and "
        "revisit once the Northwind number lands. Happy to walk through it Thursday."),
    "demo-mail-3": (
        "Re: PO 88213 overdue",
        "ap-invoices@sailpoint.com",
        "Approved. Please push this one through — the approval window lapsed "
        "while I was out."),
    "demo-mail-6": (
        "Re: SOW redlines for Northwind",
        "heidi.stark@sailpoint.com",
        "Heidi — following up on the redlines I sent Thursday. Anything legal "
        "still needs from me before this can move?"),
    "_default": (
        "Re: your message",
        "scott.chen@sailpoint.com",
        "Thanks — I'll come back to you on this shortly."),
}

# Scripted replies, keyed by the quick-action chip or by a keyword in free text.
# Phase 1 has no model behind the chat; these make the flow reviewable.
SCRIPT = {
    # Deliberately quotes no body: the approval card carries the real draft for this
    # item, and an earlier version narrated a different one than the card showed.
    "draft reply": (
        "Drafted in your voice — short, no preamble, and it commits to a date only "
        "where the thread already does.\n\n"
        "The card below holds the exact text and the exact recipient. Nothing is "
        "sent until you click Send, and you get 30 seconds to undo after that."
    ),
    "summarize": (
        "Scott needs Q4 forecast sign-off before the Thursday board pack. You said "
        "Wednesday. He asked to be told early if the EMEA number moves. "
        "One open commitment from you, no open question for him."
    ),
    "action items": (
        "1. Send Q4 forecast numbers — owed to Scott Chen, due Wednesday.\n"
        "2. Flag EMEA separately if it moves — conditional, same deadline."
    ),
    "prep brief": (
        "Northwind technical deep dive, 4 h out, you are presenting.\n\n"
        "Attendees: Priya Raman (external, their security lead), Scott Chen.\n"
        "Open threads: tenant isolation and key handling questions from two days ago.\n"
        "Gaps: no agenda posted, your RSVP is still pending, and it overlaps the "
        "Teams/SharePoint discussion at the same hour."
    ),
    "remind me": "Set. This will resurface in your morning brief tomorrow at 08:00.",
    "create task": "Task created locally: \"Send Q4 forecast numbers\", due Wednesday.",
    "propose new time": (
        "Three open slots from your calendar: Tue 14:00, Wed 09:30, Thu 11:00. "
        "Proposing a new time needs your approval before anything is sent."
    ),
    "post summary to slack": (
        "Drafted for #deal-acme. Posting to a channel is a separate approval from "
        "any mail reply — the card below covers only this post."
    ),
}

FALLBACK_REPLY = (
    "Demo mode: the chat is scripted, so this answer is canned rather than reasoned. "
    "Real per-item chat arrives in phase 3. Measured warm round-trip will be about "
    "11 s for text, 30 s when it calls a connector."
)


class DemoSource:
    """Satisfies the Connector protocol with fixtures and nothing else."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._bus: EventBus | None = None
        self._started = now_iso()

    def status(self) -> SourceStatus:
        return SourceStatus(source=self.name, health="demo", last_ok_at=self._started,
                            measured_latency_ms=0)

    def list_new(self, since: str = "") -> list[Item]:
        now = datetime.now(timezone.utc)
        items, _ = _fixtures(now)
        return [i for i in items if i.source == self.name]

    def get_thread(self, item_id: str) -> Thread:
        _, threads = _fixtures(datetime.now(timezone.utc))
        return Thread(item_id=item_id, messages=threads.get(item_id, []))

    def draft(self, item_id: str, intent: str) -> Draft:
        """Drafting never sends. There is no token here and no send call.

        The fixture is keyed by item so the approval card matches what it claims to
        reply to — an earlier version returned the same mail draft for every item,
        which showed a Q4-forecast reply addressed to a Slack channel.
        """
        _, _ = _fixtures(datetime.now(timezone.utc))
        subject, to_addr, body = DRAFTS.get(item_id, DRAFTS["_default"])
        return Draft(
            id=f"draft-{uuid.uuid4().hex[:8]}", item_id=item_id,
            channel="slack" if self.name == "slack" else "mail",
            to_addr=to_addr, subject=subject, body=body, created_at=now_iso(),
        )

    def send(self, draft_id: str, approval: ApprovalToken,
             draft: Draft | None = None) -> dict:
        require_approval(draft_id, approval)
        # Demo mode stops here on purpose: the gate is exercised, nothing leaves.
        return {"ok": True, "demo": True, "note": "demo mode — nothing was actually sent"}

    def subscribe(self, bus: EventBus) -> None:
        self._bus = bus

    @staticmethod
    def scripted_reply(prompt: str) -> str:
        low = prompt.lower().strip()
        for key, reply in SCRIPT.items():
            if key in low:
                return reply
        return FALLBACK_REPLY
