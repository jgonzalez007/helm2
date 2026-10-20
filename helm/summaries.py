"""Morning brief, end-of-day recap, weekly recap, metrics, and commitments.

All of it is computed from rows already in the store: no connector call and no model
call, so a summary costs nothing and is right offline.

Commitment extraction is deliberately deterministic. It reads **your own** sent
messages for first-person promises, with plain string matching, the same way the
voice profile does. A model would catch more, but every message would cost a turn,
and the phrasing people use for a promise is narrow enough to match.

shortcut: matches first-person promise phrasings in English only, and a date only
when the promise names a weekday, "tomorrow", "today" or "EOD"; upgrade to the
stage-2 classifier if real sent mail shows it missing obvious commitments. A clipped
sentence with no subject ("Will confirm Wednesday") counts because the input is
already only your own messages.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .flags import _deadline_from_subject

# A promise starts with the user committing to do something. "Can you send X" is
# not a commitment, and neither is "he will send X", so the subject has to be first
# person and the verb has to be future.
_PROMISE_RE = re.compile(
    r"\b(?:i['’]?ll|i will|i'?m going to|i am going to|i shall|let me|"
    r"i can get|i'?ll get|we['’]?ll|we will)\b[^.!?\n]{0,120}",
    re.I,
)

# Real sent mail drops the pronoun: "Will confirm by Wednesday", "Sending Monday".
# Safe only because the input is already filtered to messages you wrote, so a
# sentence with no subject at all is yours by construction.
# It also follows a comma: "Looking at it now, will confirm by Wednesday." Requiring
# a sentence start or a comma is what keeps "Scott will confirm" out.
_CLIPPED_RE = re.compile(r"(?:^|,\s+)((?:will|sending|shall)\b[^.!?\n]{0,120})", re.I)

# Sentences that look like promises but are not: hedges and questions.
_NOT_A_PROMISE = re.compile(r"\b(?:if you|unless|would you|could you|can you|"
                            r"should i|shall i|do you want)\b", re.I)


@dataclass
class Commitment:
    text: str
    owed_to: str = ""
    due_at: str = ""
    source_quote: str = ""
    item_id: str = ""
    state: str = "open"

    def to_dict(self) -> dict:
        return asdict(self)


def extract_commitments(messages: list[dict], now: datetime | None = None) -> list[Commitment]:
    """Promises in your own sent messages.

    `messages` are dicts with at least `body`/`text`, and optionally `to`,
    `item_id` and `sent_at`. Only messages you sent should be passed in — this
    never inspects what anyone else wrote.
    """
    now = now or datetime.now().astimezone()
    out: list[Commitment] = []
    seen: set[str] = set()

    for msg in messages:
        body = (msg.get("body") or msg.get("text") or "").strip()
        if not body:
            continue
        for sentence in re.split(r"(?<=[.!?\n])\s+", body):
            sentence = sentence.strip()
            if not sentence or _NOT_A_PROMISE.search(sentence):
                continue
            match = _PROMISE_RE.search(sentence)
            clipped = None if match else _CLIPPED_RE.search(sentence)
            if not (match or clipped):
                continue
            # The clipped pattern captures past the comma it matched on.
            raw = match.group(0) if match else clipped.group(1)
            text = " ".join(raw.split())
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            due = _deadline_from_subject(sentence, now)
            out.append(Commitment(
                text=text,
                owed_to=msg.get("to") or "",
                due_at=due.isoformat(timespec="seconds") if due else "",
                source_quote=sentence[:240],
                item_id=msg.get("item_id", ""),
            ))
    return out


# ── summaries ───────────────────────────────────────────────────────────────

def _is_today(iso: str, now: datetime) -> bool:
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso).astimezone().date() == now.date()
    except ValueError:
        return False


def _is_tomorrow(iso: str, now: datetime) -> bool:
    if not iso:
        return False
    try:
        return (datetime.fromisoformat(iso).astimezone().date()
                == (now + timedelta(days=1)).date())
    except ValueError:
        return False


def suggest_block(meetings: list[dict], now: datetime | None = None) -> dict:
    """The first clear hour tomorrow, for the important-but-not-urgent pile.

    Computed from the calendar rather than guessed, and honest when there is no gap.
    """
    now = now or datetime.now().astimezone()
    day = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    busy = []
    for m in meetings:
        try:
            start = datetime.fromisoformat(m["starts_at"]).astimezone()
            end = datetime.fromisoformat(m["ends_at"] or m["starts_at"]).astimezone()
        except (ValueError, KeyError):
            continue
        if start.date() == day.date():
            busy.append((start, end))
    busy.sort()

    cursor = day
    last = day.replace(hour=17, minute=0)
    for start, end in busy:
        if start - cursor >= timedelta(hours=1):
            break
        cursor = max(cursor, end)
    if cursor + timedelta(hours=1) > last:
        return {"found": False, "reason": "tomorrow is booked solid until 17:00"}
    return {"found": True, "start": cursor.isoformat(timespec="minutes"),
            "end": (cursor + timedelta(hours=1)).isoformat(timespec="minutes")}


def morning_brief(rows: list[dict], now: datetime | None = None) -> dict:
    """Today's meetings with their gaps, what needs a reply, what you are waiting on."""
    now = now or datetime.now().astimezone()
    meetings = [r for r in rows if r["item"]["source"] == "calendar"
                and _is_today(r["item"]["starts_at"], now)]
    meetings.sort(key=lambda r: r["item"]["starts_at"])

    needs = [r for r in rows if r["item"]["state"] == "needs_me"
             and r["item"]["source"] != "calendar"]
    waiting = [r for r in rows if r["item"]["state"] == "waiting"]
    block_items = [r for r in needs if r["flag"]["quadrant"] == "important_only"]

    return {
        "kind": "brief",
        "generated_at": now.isoformat(timespec="seconds"),
        "meetings": [{
            "id": r["item"]["id"],
            "subject": r["item"]["subject"],
            "starts_at": r["item"]["starts_at"],
            "attendees": r["item"]["attendees"],
            # Exactly the gaps a prep brief would close, named rather than implied.
            "gaps": [g for g in (
                "no agenda posted" if not r["item"]["has_agenda"] else "",
                "RSVP still pending" if r["item"]["rsvp_pending"] else "",
                "overlaps another meeting" if r["item"]["conflicts_with"] else "",
                "external attendees" if r["item"]["external_attendees"] else "",
            ) if g],
        } for r in meetings],
        "needs_reply": [r["item"]["id"] for r in needs
                        if r["flag"]["quadrant"] in ("important_urgent", "urgent_only")],
        "waiting_on": [r["item"]["id"] for r in waiting],
        "time_block": {
            "items": [r["item"]["id"] for r in block_items],
            "slot": suggest_block([r["item"] for r in rows
                                   if r["item"]["source"] == "calendar"], now),
        },
    }


def end_of_day(rows: list[dict], now: datetime | None = None) -> dict:
    """Handled, still open, and tomorrow's first meetings."""
    now = now or datetime.now().astimezone()
    handled = [r for r in rows if r["item"]["state"] == "done"]
    still_open = [r for r in rows if r["item"]["state"] == "needs_me"
                  and r["item"]["source"] != "calendar"]
    tomorrow = [r for r in rows if r["item"]["source"] == "calendar"
                and _is_tomorrow(r["item"]["starts_at"], now)]
    tomorrow.sort(key=lambda r: r["item"]["starts_at"])

    return {
        "kind": "recap",
        "generated_at": now.isoformat(timespec="seconds"),
        "handled": [r["item"]["id"] for r in handled],
        "still_open": [r["item"]["id"] for r in still_open],
        "tomorrow": [{"id": r["item"]["id"], "subject": r["item"]["subject"],
                      "starts_at": r["item"]["starts_at"]} for r in tomorrow[:3]],
    }


def weekly_metrics(rows: list[dict], log: list[dict], now: datetime | None = None) -> dict:
    """Monday's numbers, each one counted rather than estimated.

    `flag_accuracy` is the share of flags you did not have to correct. It is the only
    honest measure available locally: an override is the single signal that a flag was
    wrong, so a week with no overrides reads as 100% and says how many it is from.
    """
    now = now or datetime.now().astimezone()
    week_ago = now - timedelta(days=7)

    def recent(iso: str) -> bool:
        if not iso:
            return False
        try:
            return datetime.fromisoformat(iso).astimezone() >= week_ago
        except ValueError:
            return False

    cleared = [r for r in rows if r["item"]["state"] == "done"
               and recent(r["item"]["replied_at"] or r["item"]["received_at"])]

    # Response time: received to replied, for anything answered this week.
    gaps = []
    for r in rows:
        item = r["item"]
        if not (item["replied_at"] and item["received_at"]):
            continue
        try:
            a = datetime.fromisoformat(item["received_at"]).astimezone()
            b = datetime.fromisoformat(item["replied_at"]).astimezone()
        except ValueError:
            continue
        if b >= week_ago and b > a:
            gaps.append((b - a).total_seconds() / 3600)

    meetings = [r for r in rows if r["item"]["source"] == "calendar"
                and recent(r["item"]["starts_at"])]
    by_day: dict[str, int] = {}
    for r in meetings:
        try:
            day = datetime.fromisoformat(r["item"]["starts_at"]).astimezone().date()
        except ValueError:
            continue
        by_day[day.isoformat()] = by_day.get(day.isoformat(), 0) + 1

    flags = [r["flag"] for r in rows]
    pinned = [f for f in flags if f.get("pinned")]

    return {
        "kind": "weekly",
        "generated_at": now.isoformat(timespec="seconds"),
        "items_cleared": len(cleared),
        "median_response_hours": round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None,
        "replies_counted": len(gaps),
        "meetings_total": len(meetings),
        "meetings_per_day": round(len(meetings) / max(1, len(by_day)), 1) if by_day else 0,
        "busiest_day": max(by_day, key=by_day.get) if by_day else "",
        "flags_total": len(flags),
        "flags_corrected": len(pinned),
        "flag_accuracy": round(100 * (1 - len(pinned) / len(flags)), 1) if flags else None,
        "actions_logged": len([a for a in log if recent(a.get("at", ""))]),
    }


# ── when a summary is due ───────────────────────────────────────────────────

@dataclass
class Due:
    brief: bool = False
    recap: bool = False
    weekly: bool = False
    reasons: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return self.brief or self.recap or self.weekly


def _hhmm(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = text.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return fallback


def due_now(now: datetime, times: dict, last: dict) -> Due:
    """Which summaries are due, given the configured times and when each last ran.

    Keyed on the date it is generated for, so a machine that was asleep at 08:00
    still gets its brief when it wakes, and never gets two.
    """
    out = Due()
    today = now.date().isoformat()

    bh, bm = _hhmm((times or {}).get("brief", "08:00"), (8, 0))
    if now.hour * 60 + now.minute >= bh * 60 + bm and last.get("brief") != today:
        out.brief = True
        out.reasons.append(f"morning brief for {today}")

    rh, rm = _hhmm((times or {}).get("recap", "17:30"), (17, 30))
    if now.hour * 60 + now.minute >= rh * 60 + rm and last.get("recap") != today:
        out.recap = True
        out.reasons.append(f"end-of-day recap for {today}")

    # Monday, after the brief, once.
    if now.weekday() == 0 and now.hour * 60 + now.minute >= bh * 60 + bm \
            and last.get("weekly") != today:
        out.weekly = True
        out.reasons.append(f"weekly recap for {today}")

    return out
