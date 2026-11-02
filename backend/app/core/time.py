"""UTC helpers. Everything stored or compared internally is timezone-aware UTC.

The display timezone is a presentation concern only: the API always returns UTC
ISO-8601 timestamps and exposes the configured display timezone separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC. Naive datetimes are rejected, not guessed."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must include a timezone offset")
    return value.astimezone(UTC)


def validate_timezone_name(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc
    return name
