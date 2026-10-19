"""Stage-1 flag rules: deterministic, local, free.

The security property this file exists to guarantee:

    A message cannot influence its own flag.

It is enforced structurally, not by discipline. `_signals()` reads an Item and
returns only metadata — sender identity, recipient position, calendar times,
thread state, and dates from *structured* fields. `_score()` takes that signal
dict and never sees the Item, so there is no expression in the scoring path that
can reach `item.snippet` or a message body.

The body is read in exactly one place, `posture_warning()`, which can only write
to `Flag.posture_warning` and can never move importance or urgency.

Stage 2 (Claude, for ambiguous leftovers) arrives in phase 5. `needs_claude()`
already marks the items that will go.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import Flag, Item, now_iso

# Phrases that get used to manufacture urgency. Finding one NEVER raises a flag.
# It can only produce a posture warning, and only from an external or unknown
# sender. See CLAUDE.md.
PRESSURE_PHRASES = (
    "urgent",
    "immediately",
    "asap",
    "act now",
    "action required",
    "high priority",
    "time sensitive",
    "final notice",
    "expires today",
    "verify your account",
    "within 24 hours",
    "do not ignore",
    "failure to respond",
)

BULK_HINTS = ("no-reply", "noreply", "donotreply", "notifications@", "mailer@", "bounce")


@dataclass
class Settings:
    """What the rules are allowed to know. All of it is the user's own config."""

    vips: set[str] = field(default_factory=set)
    managers: set[str] = field(default_factory=set)
    muted_senders: set[str] = field(default_factory=set)
    muted_channels: set[str] = field(default_factory=set)
    internal_domains: set[str] = field(default_factory=set)
    # Learned locally from "not important" / "more urgent" clicks.
    keyword_weights: dict[str, int] = field(default_factory=dict)
    # What HELM has learned about individual senders, from a click or from stage 2.
    # Keyed by lowercase address. See store.sender_profile.
    sender_profiles: dict[str, dict] = field(default_factory=dict)
    waiting_days: int = 3

    def normalized(self) -> "Settings":
        low = lambda s: {x.lower() for x in s}  # noqa: E731
        return Settings(
            vips=low(self.vips),
            managers=low(self.managers),
            muted_senders=low(self.muted_senders),
            muted_channels=low(self.muted_channels),
            internal_domains=low(self.internal_domains),
            keyword_weights={k.lower(): v for k, v in self.keyword_weights.items()},
            sender_profiles={k.lower(): v for k, v in self.sender_profiles.items()},
            waiting_days=self.waiting_days,
        )


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _signals(item: Item, cfg: Settings, now: datetime) -> dict:
    """Metadata only. Deliberately does not read snippet or any message body."""
    addr = (item.sender_addr or "").lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    # The subject is a structured field, so a date in it is allowed as a
    # deadline signal — but only the date, never the surrounding words.
    subject_deadline = _deadline_from_subject(item.subject, now)
    deadline = _parse(item.deadline_at) or subject_deadline

    profile = cfg.sender_profiles.get(addr) or {}
    starts = _parse(item.starts_at)
    sent = _parse(item.sent_by_me_at)
    reply = _parse(item.last_reply_at)

    return {
        "source": item.source,
        "kind": item.kind,
        "is_vip": addr in cfg.vips,
        "is_manager": addr in cfg.managers,
        "is_muted": addr in cfg.muted_senders or item.thread_id.lower() in cfg.muted_channels,
        "is_bulk": any(h in addr for h in BULK_HINTS),
        "is_external": bool(item.sender_external or (domain and domain not in cfg.internal_domains)),
        "to_me_direct": bool(item.to_me_direct),
        "direct_ping": item.kind in ("dm", "mention"),
        # Organizing something with nobody else in it is a focus block, not a
        # meeting; it should not outrank a real one in the queue.
        "organizer_or_presenter": bool(
            (item.organizer_is_me and item.attendees) or item.presenter_is_me),
        "solo_block": bool(item.organizer_is_me and not item.attendees),
        "external_attendees": bool(item.external_attendees),
        # Learned once, applied free thereafter. This is what replaces guessing
        # from an address pattern on every sweep.
        "known_automated": bool(profile.get("is_automated")),
        "known_expects_reply": bool(profile.get("expects_reply", True)),
        "known_importance": str(profile.get("importance_hint") or ""),
        "known_pinned": bool(profile.get("pinned")),
        "known_reason": str(profile.get("reason") or ""),
        "all_day": bool(item.all_day),
        "rsvp_pending": bool(item.rsvp_pending),
        "has_agenda": bool(item.has_agenda),
        "has_conflict": bool(item.conflicts_with),
        "hours_to_deadline": _hours(deadline, now),
        "minutes_to_start": _minutes(starts, now),
        "awaiting_reply_days": _days_awaiting(sent, reply, now),
        "keyword_weight": _keyword_weight(item.subject, cfg),
        "state": item.state,
    }


def _hours(dt: datetime | None, now: datetime) -> float | None:
    return None if dt is None else (dt - now).total_seconds() / 3600


def _minutes(dt: datetime | None, now: datetime) -> float | None:
    return None if dt is None else (dt - now).total_seconds() / 60


def _days_awaiting(sent: datetime | None, reply: datetime | None, now: datetime) -> float | None:
    if sent is None or reply is not None:
        return None
    return (now - sent).total_seconds() / 86400


def _keyword_weight(subject: str, cfg: Settings) -> int:
    low = (subject or "").lower()
    return sum(w for kw, w in cfg.keyword_weights.items() if kw in low)


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")


def _deadline_from_subject(subject: str, now: datetime) -> datetime | None:
    """A date in the subject line, as a date. The words around it are ignored.

    shortcut: handles "by <weekday>", "<weekday> EOD" and d/m numeric dates only,
    which is what real subject lines carry; upgrade to a full date parser if
    subjects start using "the 3rd of next month".
    """
    low = (subject or "").lower()

    m = _DATE_RE.search(low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or now.year)
        if year < 100:
            year += 2000
        # Subjects are written day-first or month-first depending on the author;
        # take whichever ordering yields a real date nearest in the future.
        for d, mo in ((day, month), (month, day)):
            try:
                cand = now.replace(year=year, month=mo, day=d, hour=17, minute=0,
                                   second=0, microsecond=0)
            except ValueError:
                continue
            return cand

    for i, name in enumerate(_WEEKDAYS):
        if name in low:
            ahead = (i - now.weekday()) % 7
            if ahead == 0:
                ahead = 7 if "next" in low else 0
            return (now + timedelta(days=ahead)).replace(hour=17, minute=0, second=0, microsecond=0)

    if "tomorrow" in low:
        return (now + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    if "today" in low or "eod" in low:
        return now.replace(hour=17, minute=0, second=0, microsecond=0)
    return None


def _score(sig: dict) -> tuple[str, str, list[dict], bool]:
    """Signals in, (importance, urgency, reasons, confident) out.

    Has no access to the Item, and therefore none to any message body.
    """
    reasons: list[dict] = []
    importance = "normal"
    urgency = "none"
    confident = False

    # ── Importance ──────────────────────────────────────────────────────────
    if sig["known_pinned"]:
        importance = sig["known_importance"] or ("low" if sig["known_automated"] else "normal")
        reasons.append({"rule": "your_override",
                        "detail": sig["known_reason"] or "you set this sender yourself"})
        confident = True
    elif sig["known_automated"]:
        importance = sig["known_importance"] or "low"
        reasons.append({"rule": "known_sender",
                        "detail": sig["known_reason"] or "a system or digest sender"})
        confident = True
    elif sig["is_muted"]:
        importance = "low"
        reasons.append({"rule": "muted", "detail": "sender or channel is muted"})
        confident = True
    elif sig["is_bulk"]:
        importance = "low"
        reasons.append({"rule": "bulk", "detail": "bulk or no-reply address"})
        confident = True
    elif sig["is_manager"]:
        importance = "high"
        reasons.append({"rule": "manager", "detail": "sender is on your manager list"})
        confident = True
    elif sig["is_vip"]:
        importance = "high"
        reasons.append({"rule": "vip", "detail": "sender is on your VIP list"})
        confident = True
    elif sig["direct_ping"]:
        importance = "high"
        reasons.append({"rule": "direct", "detail": "direct message or @you mention"})
        confident = True
    elif sig["source"] == "calendar" and (sig["organizer_or_presenter"] or sig["external_attendees"]):
        importance = "high"
        which = "you organize or present" if sig["organizer_or_presenter"] else "external attendees"
        reasons.append({"rule": "meeting_role", "detail": which})
        confident = True
    elif not sig["to_me_direct"]:
        importance = "normal"
        reasons.append({"rule": "cc_only", "detail": "you are on Cc, not To"})
        confident = True

    weight = sig["keyword_weight"]
    if weight:
        reasons.append({"rule": "keywords", "detail": f"learned keyword weight {weight:+d}"})
        if weight > 0 and importance == "normal":
            importance = "high"
            confident = True
        elif weight < 0 and importance == "normal":
            importance = "low"
            confident = True

    # ── Urgency ─────────────────────────────────────────────────────────────
    # An all-day block is an absence, not a meeting: no countdown urgency,
    # no agenda gap, no RSVP gap.
    mins = None if sig["all_day"] else sig["minutes_to_start"]
    if mins is not None and 0 <= mins <= 15:
        urgency = "now"
        reasons.append({"rule": "meeting_imminent", "detail": f"starts in {int(mins)} min"})
        confident = True
    elif mins is not None and 0 <= mins <= 1440 and (sig["rsvp_pending"] or not sig["has_agenda"]):
        urgency = "today"
        gap = "RSVP still pending" if sig["rsvp_pending"] else "no agenda posted"
        reasons.append({"rule": "meeting_gap", "detail": f"{gap}, under 24 h out"})
        confident = True
    elif mins is not None and 0 <= mins <= 1440 and sig["has_conflict"]:
        urgency = "today"
        reasons.append({"rule": "meeting_conflict", "detail": "overlaps another meeting"})
        confident = True

    hrs = sig["hours_to_deadline"]
    if hrs is not None and urgency == "none":
        if hrs <= 24:
            urgency = "now"
            reasons.append({"rule": "deadline", "detail": f"deadline in {max(0, int(hrs))} h"})
        elif hrs <= 72:
            urgency = "today"
            reasons.append({"rule": "deadline", "detail": f"deadline in {int(hrs / 24)} d"})
        elif hrs <= 168:
            urgency = "week"
            reasons.append({"rule": "deadline", "detail": f"deadline in {int(hrs / 24)} d"})
        if urgency != "none":
            confident = True

    days = sig["awaiting_reply_days"]
    if days is not None and days >= 3 and urgency == "none":
        urgency = "week"
        reasons.append({"rule": "waiting_on", "detail": f"sent {int(days)} d ago, no reply"})
        confident = True

    if importance == "low" and urgency == "none":
        confident = True

    return importance, urgency, reasons, confident


def posture_warning(item: Item, cfg: Settings) -> str:
    """Pressure language from an unknown sender. Affects display only.

    Returning a non-empty string must never change importance or urgency —
    `classify()` assigns it to Flag.posture_warning and nothing else.
    """
    addr = (item.sender_addr or "").lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    external = bool(item.sender_external or (domain and domain not in cfg.internal_domains))
    known = addr in cfg.vips or addr in cfg.managers
    if not external or known:
        return ""
    body = f"{item.subject} {item.snippet}".lower()
    hit = next((p for p in PRESSURE_PHRASES if p in body), "")
    if not hit:
        return ""
    return (
        f'Pressure language ("{hit}") from an external sender. '
        f"Real address: {item.sender_addr or 'unknown'}. "
        "These words did not raise this item's flags."
    )


def needs_claude(sig: dict, confident: bool) -> bool:
    """Stage 2 only sees what stage 1 could not settle."""
    return not confident


def _recheck_after(sig: dict, now: datetime) -> str:
    """When urgency could change on its own, with no model call and no new tokens."""
    candidates = []
    if sig["hours_to_deadline"] is not None:
        for threshold in (168, 72, 24):
            remaining = sig["hours_to_deadline"] - threshold
            if remaining > 0:
                candidates.append(now + timedelta(hours=remaining))
    if sig["minutes_to_start"] is not None and sig["minutes_to_start"] > 15:
        candidates.append(now + timedelta(minutes=sig["minutes_to_start"] - 15))
    if not candidates:
        return (now + timedelta(hours=12)).isoformat(timespec="seconds")
    return min(candidates).isoformat(timespec="seconds")


def classify(item: Item, cfg: Settings, now: datetime | None = None,
             existing: Flag | None = None) -> Flag:
    """The only entry point. A pinned flag is returned untouched."""
    if existing is not None and existing.pinned:
        return existing

    now = now or datetime.now(timezone.utc)
    cfg = cfg.normalized()
    sig = _signals(item, cfg, now)
    importance, urgency, reasons, confident = _score(sig)

    return Flag(
        item_id=item.id,
        importance=importance,
        urgency=urgency,
        reasons=reasons,
        decided_by="rules",
        confidence=None,
        pinned=False,
        posture_warning=posture_warning(item, cfg),
        evaluated_at=now_iso(),
        recheck_after=_recheck_after(sig, now),
    )


def override(flag: Flag, kind: str) -> Flag:
    """One-click feedback. The result is pinned and never reclassified."""
    steps_imp = ["low", "normal", "high"]
    steps_urg = ["none", "week", "today", "now"]
    reasons = list(flag.reasons)

    if kind == "not_important":
        flag.importance = "low"
        reasons.append({"rule": "override", "detail": "you marked this not important"})
    elif kind == "more_important":
        i = min(len(steps_imp) - 1, steps_imp.index(flag.importance) + 1)
        flag.importance = steps_imp[i]
        reasons.append({"rule": "override", "detail": "you raised importance"})
    elif kind == "more_urgent":
        i = min(len(steps_urg) - 1, steps_urg.index(flag.urgency) + 1)
        flag.urgency = steps_urg[i]
        reasons.append({"rule": "override", "detail": "you raised urgency"})
    elif kind == "less_urgent":
        i = max(0, steps_urg.index(flag.urgency) - 1)
        flag.urgency = steps_urg[i]
        reasons.append({"rule": "override", "detail": "you lowered urgency"})
    else:
        raise ValueError(f"unknown override {kind!r}")

    flag.reasons = reasons
    flag.pinned = True
    flag.decided_by = "rules"
    flag.confidence = None
    flag.evaluated_at = now_iso()
    return flag


# Focus queue order: important+urgent first, then important, then urgent, then rest.
QUADRANT_RANK = {"important_urgent": 0, "important_only": 1, "urgent_only": 2, "neither": 3}
URGENCY_RANK = {"now": 0, "today": 1, "week": 2, "none": 3}


def focus_sort_key(flag: Flag, item: Item) -> tuple:
    return (
        QUADRANT_RANK[flag.quadrant],
        URGENCY_RANK[flag.urgency],
        item.received_at or item.starts_at or "",
    )
