from __future__ import annotations

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def utc_day_start_iso() -> str:
    """Start of the current UTC day, in the same format as utcnow_iso().

    Comparable with stored timestamps as a plain string, since ISO-8601 with a
    fixed offset sorts lexicographically.
    """
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat(timespec="seconds")


def hours_between(earlier: str, later: str) -> float:
    """Hours from one stored timestamp to another, or 0 if either is unusable.

    Unparseable means a hand-edited or truncated row, and the safe reading of
    "we cannot tell how long ago that was" is "not long".
    """
    try:
        start = datetime.fromisoformat(earlier)
        end = datetime.fromisoformat(later)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600)
