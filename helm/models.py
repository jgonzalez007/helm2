"""The shapes every source, the store and the UI agree on."""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

Source = Literal["mail", "slack", "calendar"]
Kind = Literal["email", "dm", "mention", "thread", "event"]
Importance = Literal["high", "normal", "low"]
Urgency = Literal["now", "today", "week", "none"]
State = Literal["needs_me", "working", "done", "waiting", "snoozed"]
Health = Literal["live", "reconnecting", "stale", "unavailable", "demo"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Item:
    """One email, Slack message/thread or calendar event.

    `id` is the source's own stable id, so re-syncing updates the row instead of
    creating a twin, and reading something elsewhere can retire it.
    """

    id: str
    source: Source
    kind: Kind
    subject: str
    sender: str = ""
    sender_addr: str = ""
    sender_external: bool = False
    snippet: str = ""
    thread_id: str = ""
    received_at: str = ""
    starts_at: str = ""
    ends_at: str = ""
    state: State = "needs_me"
    # Calendar-only, and the handful of fields the flag rules need from
    # structured metadata rather than from the body.
    to_me_direct: bool = True        # on To:/@you rather than Cc:/channel noise
    organizer_is_me: bool = False
    presenter_is_me: bool = False
    rsvp_pending: bool = False
    has_agenda: bool = True
    all_day: bool = False
    attendees: list[str] = field(default_factory=list)
    external_attendees: bool = False
    join_url: str = ""
    conflicts_with: list[str] = field(default_factory=list)
    # A deadline parsed from the subject line or structured metadata ONLY.
    # Never from the body — see CLAUDE.md.
    deadline_at: str = ""
    sent_by_me_at: str = ""          # feeds the waiting-on tracker
    last_reply_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Flag:
    """Two independent axes, plus why, plus who decided."""

    item_id: str
    importance: Importance = "normal"
    urgency: Urgency = "none"
    reasons: list[dict] = field(default_factory=list)   # [{rule, detail}]
    decided_by: Literal["rules", "claude"] = "rules"
    confidence: float | None = None
    pinned: bool = False
    posture_warning: str = ""        # pressure language from an unknown sender
    evaluated_at: str = ""
    recheck_after: str = ""

    @property
    def quadrant(self) -> str:
        important = self.importance == "high"
        urgent = self.urgency in ("now", "today")
        if important and urgent:
            return "important_urgent"
        if important:
            return "important_only"
        if urgent:
            return "urgent_only"
        return "neither"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quadrant"] = self.quadrant
        return d


@dataclass
class SourceStatus:
    source: str
    health: Health = "stale"
    last_ok_at: str = ""
    last_error: str = ""
    measured_latency_ms: int = 0
    count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Thread:
    item_id: str
    messages: list[dict] = field(default_factory=list)   # [{author, body, sent_at, is_me}]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Draft:
    id: str
    item_id: str
    channel: str                 # mail | slack
    to_addr: str
    subject: str
    body: str
    created_at: str = ""
    status: str = "drafted"      # drafted | approved | sent | undone | failed

    def to_dict(self) -> dict:
        return asdict(self)


class ApprovalToken:
    """Proof that a human clicked.

    `Connector.send()` refuses to run without one. Minting lives behind
    `mint_from_click()` so that grepping for the mint site shows every place a
    send can begin — today, exactly one: the approval-card click handler.
    """

    __slots__ = ("draft_id", "value", "minted_at")

    def __init__(self, draft_id: str, value: str, minted_at: str) -> None:
        self.draft_id = draft_id
        self.value = value
        self.minted_at = minted_at

    @classmethod
    def mint_from_click(cls, draft_id: str) -> "ApprovalToken":
        return cls(draft_id, secrets.token_urlsafe(16), now_iso())
