"""Small helpers shared by the HTTP adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.providers.types import MalformedResponseError


def from_unix(value: object, what: str, *, millis: bool = False) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MalformedResponseError(f"{what} is not a unix timestamp")
    seconds = value / 1000 if millis else value
    return datetime.fromtimestamp(seconds, UTC)


def parse_date(value: object, what: str) -> datetime:
    """'YYYY-MM-DD' (or a full ISO timestamp) -> midnight UTC of that day."""
    if not isinstance(value, str):
        raise MalformedResponseError(f"{what} is not a date string")
    try:
        day = date.fromisoformat(value[:10])
    except ValueError as exc:
        raise MalformedResponseError(f"{what} is not a date") from exc
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def parse_iso_datetime(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise MalformedResponseError(f"{what} is not a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedResponseError(f"{what} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def day_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def is_closed_period(period_end: datetime, now: datetime, settle: timedelta) -> bool:
    """A period is treated as final once it ended more than ``settle`` ago."""
    return period_end <= now - settle
