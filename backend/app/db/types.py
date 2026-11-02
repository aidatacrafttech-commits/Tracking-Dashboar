"""Custom column types that keep SQLite storage exact and timezone-safe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.money import from_storage_int, to_storage_int
from app.core.time import ensure_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, returns aware UTC. Rejects naive input instead of guessing."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class ScaledDecimal(TypeDecorator[Decimal]):
    """Exact decimal stored as a 64-bit integer of micro-units (value * 10**6).

    Integer storage keeps SQL comparisons and SUM() exact and portable; SQLite has no
    native DECIMAL and would otherwise fall back to floating point.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | str | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("floats are not accepted for monetary values; use Decimal")
        return to_storage_int(Decimal(value))

    def process_result_value(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return from_storage_int(int(value))


class JSONText(TypeDecorator[Any]):
    """JSON stored as TEXT with deterministic key order."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return json.loads(value)
