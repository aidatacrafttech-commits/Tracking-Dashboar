"""Course access validity: calendar arithmetic, never "30-day months".

A student's access starts at their own successful purchase and ends `value` days / months /
years later on the calendar of the display timezone (IST by default). So a purchase on
10 Nov 2026 (IST) with 6 months of validity expires on 10 May 2027 (IST) at the same time of
day, and 31 Jan + 1 month ends on the last day of February.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.enums import ValidityUnit

# Upper bounds per unit (10 years in every unit).
MAX_VALIDITY = {ValidityUnit.DAYS: 3650, ValidityUnit.MONTHS: 120, ValidityUnit.YEARS: 10}
# The presets offered in the dashboard (custom values are allowed too).
PRESETS = (
    (6, ValidityUnit.MONTHS),
    (7, ValidityUnit.MONTHS),
    (10, ValidityUnit.MONTHS),
    (1, ValidityUnit.YEARS),
)


def add_months(moment: datetime, months: int) -> datetime:
    """Same day-of-month `months` later, clamped to the month's last day; keeps the time
    of day and the timezone of `moment`."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def access_expiry(start: datetime, value: int, unit: ValidityUnit, timezone: str) -> datetime:
    """Expiry instant (UTC) for access starting at `start` (an aware datetime)."""
    if value < 1 or value > MAX_VALIDITY[unit]:
        raise ValueError(f"validity must be 1-{MAX_VALIDITY[unit]} {unit.value}")
    local = start.astimezone(ZoneInfo(timezone))
    if unit is ValidityUnit.DAYS:
        end = local + timedelta(days=value)
    else:
        end = add_months(local, value * 12 if unit is ValidityUnit.YEARS else value)
    return end.astimezone(UTC)


def validity_label(value: int, unit: ValidityUnit) -> str:
    name = {ValidityUnit.DAYS: "day", ValidityUnit.MONTHS: "month", ValidityUnit.YEARS: "year"}[unit]
    return f"{value} {name}{'' if value == 1 else 's'}"
