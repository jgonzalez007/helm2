"""ClaudeConnectorSource — real data through the user's approved connectors.

Phase 2 implements calendar only, read-only. Mail is phase 3, Slack phase 4.

Every value that arrives here came from a calendar entry somebody else may have
written, so nothing is trusted: fields are coerced to the expected type, strings
are length-capped, and a join URL that is not http(s) is dropped rather than
stored. See `_clean_event`.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Iterable

from .. import claude_cli as cli
from ..bus import EventBus
from ..models import ApprovalToken, Draft, Item, SourceStatus, Thread, now_iso
from ._clean import (MAX_LIST, MAX_STR, _bool, _is_external, _list_of_str, _s,
                     _safe_url, timezone_mismatch, to_local_iso)
from .base import SendRefused, require_approval

# Measured: this shape takes ~124 s and returns 25 events. An earlier version also
# asked for a body excerpt, attendee display names and location, which pushed every
# sweep past 180 s. Anything HELM can derive locally is derived locally.
CALENDAR_PROMPT = """Call outlook_calendar_search with query "*", afterDateTime "{after}", \
beforeDateTime "{before}", limit 25. Make exactly one tool call and do not read any \
event resource.

Reply with ONLY a JSON array from that one result, one object per event, keys exactly:
  id              the event's identifier from the tool
  subject         the event subject
  start           the tool's start dateTime string, copied verbatim
  end             the tool's end dateTime string, copied verbatim
  timezone        the tool's timeZone string, copied verbatim
  organizer       the organizer's display name
  organizer_email the organizer's email address
  attendee_emails array of attendee email addresses
  response_status one of "organizer", "accepted", "tentative", "declined", "none"
  join_url        the Teams or other meeting join link, or an empty string
  has_agenda      true only if the result shows body content for the event
  is_cancelled    true if the event is cancelled

Do not convert any time zone and do not reformat the timestamps. Use "" or [] for
anything the result does not contain. If there are no events, reply with [].
"""

PREP_PROMPT = """Write a short prep brief for this meeting. Use only what is below \
plus, if it helps, a search of my own mail for messages with these attendees about \
this subject.

Meeting: {subject}
When: {when}
Organizer: {organizer}
Attendees: {attendees}
Agenda: {agenda}
Gaps: {gaps}

Cover, in this order and no more than 150 words total:
1. What this meeting is for, in one line.
2. Who is in the room that matters, and why.
3. Open threads or commitments relevant to it.
4. What I should walk in ready to say or decide.

Plain text, no headings, no preamble. If something is unknown, say so rather than \
inventing it.
"""


def is_all_day(start: str, end: str) -> bool:
    """Midnight to midnight across a day or more: an OOO block, not a meeting.

    These crowd a two-day window and must not get a countdown, a Join button, or a
    "no agenda" urgency bump — none of which mean anything for an absence.
    """
    if not start or not end:
        return False
    try:
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return False
    midnight = (s.hour, s.minute, s.second) == (0, 0, 0) == (e.hour, e.minute, e.second)
    return midnight and (e - s) >= timedelta(hours=23)


def _clean_event(raw, me: str, internal_domains: Iterable[str]) -> Item | None:
    if not isinstance(raw, dict):
        return None
    start = to_local_iso(raw.get("start"))
    subject = _s(raw.get("subject")) or "(no subject)"
    if not start:
        return None
    # A cancelled meeting is not something to attend, prepare for, or RSVP to. The
    # first live run showed three of them, two flagged urgent for a pending RSVP.
    if _bool(raw.get("is_cancelled")):
        return None

    event_id = _s(raw.get("id"), 200)
    if not event_id:
        # Without a stable id a resync would duplicate the row, so derive one that
        # is stable for the same event.
        event_id = f"cal-{abs(hash((subject, start))):x}"

    emails = _list_of_str(raw.get("attendee_emails"))
    organizer_email = _s(raw.get("organizer_email"), 200)
    status = _s(raw.get("response_status"), 20).lower()
    me_lower = me.lower()
    end = to_local_iso(raw.get("end"))
    all_day = is_all_day(start, end)

    return Item(
        id=event_id,
        source="calendar",
        kind="event",
        subject=subject,
        sender=_s(raw.get("organizer"), 120) or organizer_email or "unknown organizer",
        sender_addr=organizer_email,
        sender_external=_is_external(organizer_email, internal_domains),
        snippet=_s(raw.get("agenda_excerpt"), 300),
        thread_id=event_id,
        starts_at=start,
        ends_at=end,
        all_day=all_day,
        # Display names are not fetched: deriving them from the addresses costs a
        # local split instead of a slower, larger connector reply.
        attendees=_list_of_str(raw.get("attendees")) or [e.split("@")[0].replace(".", " ").title()
                                                         for e in emails],
        external_attendees=any(_is_external(e, internal_domains) for e in emails
                               if e.lower() != me_lower),
        organizer_is_me=_bool(raw.get("is_organizer")) or organizer_email.lower() == me_lower,
        # An absence has no RSVP and no agenda; treating it as a gap would put every
        # OOO block in the focus queue.
        rsvp_pending=False if all_day else status in ("none", ""),
        has_agenda=True if all_day else _bool(raw.get("has_agenda")),
        join_url=_safe_url(raw.get("join_url")),
        to_me_direct=True,
    )


def mark_conflicts(items: list[Item]) -> list[Item]:
    """Overlap is computed locally: exact, free, and right even when offline."""
    spans = []
    for item in items:
        # A week-long OOO block is not a double-booking. Including them reported
        # "overlaps 24" on the first live run and buried the real clashes.
        if item.all_day:
            continue
        try:
            s = datetime.fromisoformat(item.starts_at)
            e = datetime.fromisoformat(item.ends_at) if item.ends_at else s + timedelta(minutes=30)
        except ValueError:
            continue
        spans.append((item, s, e))

    for i, (a, a_start, a_end) in enumerate(spans):
        for b, b_start, b_end in spans[i + 1:]:
            if a_start < b_end and b_start < a_end:
                if b.id not in a.conflicts_with:
                    a.conflicts_with.append(b.id)
                if a.id not in b.conflicts_with:
                    b.conflicts_with.append(a.id)
    return items


class CalendarSource:
    """Read-only calendar. Phase 2 has no draft and no send."""

    name = "calendar"

    def __init__(self, me: str, internal_domains: Iterable[str],
                 availability: cli.Availability | None = None,
                 lookahead_days: int = 2) -> None:
        self.me = me
        self.internal_domains = list(internal_domains)
        self.availability = availability
        self.lookahead_days = lookahead_days
        self._status = SourceStatus(source=self.name, health="stale")
        self._bus: EventBus | None = None
        self._tz_warning = ""
        self._session: cli.Session | None = None

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

        now = datetime.now().astimezone()
        prompt = CALENDAR_PROMPT.format(
            after=now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%S"),
            before=(now + timedelta(days=self.lookahead_days)).strftime("%Y-%m-%dT%H:%M:%S"),
            me=self.me,
        )

        try:
            reply, elapsed = cli.run_once(prompt, cli.CALENDAR_READ)
        except cli.ClaudeUnavailable as e:
            self._status = SourceStatus(source=self.name, health="reconnecting",
                                        last_error=str(e)[:300],
                                        last_ok_at=self._status.last_ok_at)
            return []

        parsed = cli.extract_json(reply)
        if not isinstance(parsed, list):
            self._status = SourceStatus(
                source=self.name, health="reconnecting",
                last_error=f"could not read the reply as JSON: {reply[:160]}",
                last_ok_at=self._status.last_ok_at, measured_latency_ms=elapsed)
            return []

        items = [it for it in (_clean_event(raw, self.me, self.internal_domains)
                               for raw in parsed) if it is not None]
        mark_conflicts(items)

        self._tz_warning = next(
            (w for w in (timezone_mismatch(r.get("timezone")) for r in parsed
                         if isinstance(r, dict)) if w), "")
        self._status = SourceStatus(source=self.name, health="live", last_ok_at=now_iso(),
                                    last_error=self._tz_warning,
                                    measured_latency_ms=elapsed, count=len(items))
        return items

    def get_thread(self, item_id: str) -> Thread:
        # A calendar event has no message thread. Related mail is pulled by the prep
        # brief, on request, rather than fetched for every event on every sweep.
        return Thread(item_id=item_id, messages=[])

    def draft(self, item_id: str, intent: str) -> Draft:
        raise NotImplementedError("the calendar source is read-only until phase 10")

    def send(self, draft_id: str, approval: ApprovalToken) -> dict:
        # Still check the gate: a read-only source must refuse for the right reason.
        require_approval(draft_id, approval)
        raise SendRefused("the calendar source is read-only until phase 10")

    def subscribe(self, bus: EventBus) -> None:
        self._bus = bus

    # ── Prep brief ──────────────────────────────────────────────────────────

    def session(self) -> cli.Session:
        """One warm process for interactive work, shared across prep briefs."""
        if self._session is None:
            self._session = cli.Session(cli.CALENDAR_READ + cli.MAIL_READ)
        return self._session

    def prep_brief(self, item: Item, on_token=None) -> str:
        gaps = []
        if not item.has_agenda:
            gaps.append("no agenda posted")
        if item.rsvp_pending:
            gaps.append("your RSVP is still pending")
        if item.conflicts_with:
            gaps.append("it overlaps another meeting")
        if item.external_attendees:
            gaps.append("external attendees")

        prompt = PREP_PROMPT.format(
            subject=item.subject,
            when=f"{item.starts_at} to {item.ends_at or 'unknown'}",
            organizer=f"{item.sender} <{item.sender_addr}>",
            attendees=", ".join(item.attendees) or "none listed",
            agenda=item.snippet or "none",
            gaps="; ".join(gaps) or "none",
        )
        return self.session().ask(prompt, on_token=on_token)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
