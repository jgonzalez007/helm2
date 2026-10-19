"""Coercion helpers shared by every connector source.

Everything a connector returns came from a calendar entry, a subject line or a
display name that somebody else wrote. None of it is trusted: each field is forced
to its expected type, every string is length-capped, every list is bounded, and a
URL that is not http(s) is discarded rather than stored.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

MAX_STR = 400
MAX_LIST = 40


def _s(value, limit: int = MAX_STR) -> str:
    """Any scalar to a bounded string. Never trusts the type it was given."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()[:limit]


def _list_of_str(value, limit: int = MAX_LIST) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_s(v, 120) for v in value[:limit] if _s(v, 120)]


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


# Graph returns seven fractional digits ("2026-10-16T00:00:00.0000000"), which
# datetime.fromisoformat cannot parse and an earlier version of this regex rejected
# outright — dropping every event. Seconds and fraction are both optional, and a
# trailing Z or offset is refused because these are wall-clock strings by contract.
_WALL = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?$")


def to_local_iso(wall: str) -> str:
    """A wall-clock string becomes an offset-aware local ISO timestamp.

    shortcut: assumes the Outlook mailbox time zone matches this machine's, which
    `timezone_mismatch` checks and reports; upgrade to a Windows-to-IANA mapping if
    the user ever travels with a mailbox set to another zone.
    """
    m = _WALL.match(_s(wall, 40).replace(" ", "T"))
    if not m:
        return ""
    wall = m.group(1)
    if len(wall) == 16:
        wall += ":00"
    try:
        # A naive datetime's .astimezone() reads it as local time, which is exactly
        # what a wall-clock string in the mailbox's own zone means.
        return datetime.fromisoformat(wall).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


# Mail comes back as a real instant ("2026-10-19T18:39:52.000Z") while calendar comes
# back as wall clock in the mailbox's zone. Same connector, two shapes, and they must
# not be parsed the same way: converting a wall clock would shift every meeting, and
# not converting an instant would show every email at the wrong hour.
_ABSOLUTE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?"
                       r"(Z|[+-]\d{2}:?\d{2})$")


def absolute_to_local(stamp: str) -> str:
    """A timestamp that carries Z or an offset, converted to local time."""
    m = _ABSOLUTE.match(_s(stamp, 48).replace(" ", "T"))
    if not m:
        return ""
    base, zone = m.group(1), m.group(2)
    if len(base) == 16:
        base += ":00"
    if zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        return datetime.fromisoformat(base + zone).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return ""


def any_time_to_local(stamp: str) -> str:
    """Accepts either shape. Use where the connector has been seen to return both."""
    return absolute_to_local(stamp) or to_local_iso(stamp)


def timezone_mismatch(reported: str) -> str:
    """Empty when the mailbox zone matches this machine, a warning when it does not."""
    reported = _s(reported, 64)
    if not reported:
        return ""
    local_names = {n for n in time.tzname if n}
    if reported in local_names:
        return ""
    return (f"Outlook reports times in {reported!r} but this machine is "
            f"{'/'.join(sorted(local_names)) or 'unknown'}. Meeting times may be off; "
            "times are read as local.")


def _safe_url(value) -> str:
    """A link HELM might open in a browser, so anything but http(s) is discarded."""
    url = _s(value, 2048)
    return url if url.lower().startswith(("http://", "https://")) else ""


def _is_external(email: str, internal_domains) -> bool:
    email = (email or "").lower()
    if "@" not in email:
        return False
    return email.rsplit("@", 1)[-1] not in {d.lower() for d in internal_domains}
