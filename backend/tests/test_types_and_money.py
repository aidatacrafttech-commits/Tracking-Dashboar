from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.money import check_decimal, from_storage_int, strip_trailing_zeros, to_storage_int
from app.db.types import ScaledDecimal, UTCDateTime


@pytest.mark.parametrize(
    "value",
    ["0", "0.000001", "0.1", "12345.678901", "-42.5", "999999999999.999999"],
)
def test_scaled_decimal_round_trip_is_exact(value: str) -> None:
    assert from_storage_int(to_storage_int(Decimal(value))) == Decimal(value)


def test_scaled_decimal_rejects_float_and_excess_precision() -> None:
    col = ScaledDecimal()
    with pytest.raises(TypeError, match="floats are not accepted"):
        col.process_bind_param(0.1, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most 6 decimal places"):
        col.process_bind_param(Decimal("0.0000001"), None)  # type: ignore[arg-type]
    assert col.process_bind_param(None, None) is None  # type: ignore[arg-type]
    assert col.process_result_value(None, None) is None  # type: ignore[arg-type]


def test_check_decimal_rules() -> None:
    with pytest.raises(ValueError, match="negative"):
        check_decimal(Decimal("-1"))
    with pytest.raises(ValueError, match="greater than zero"):
        check_decimal(Decimal("0"), allow_zero=False)
    with pytest.raises(ValueError, match="finite"):
        check_decimal(Decimal("NaN"))
    with pytest.raises(ValueError, match="smaller than"):
        check_decimal(Decimal("1e13"))
    assert check_decimal(Decimal("10.50"), max_places=2) == Decimal("10.50")


def test_strip_trailing_zeros_never_uses_exponent() -> None:
    assert str(strip_trailing_zeros(Decimal("100.000000"))) == "100"
    assert str(strip_trailing_zeros(Decimal("0.500000"))) == "0.5"


def test_utc_datetime_rejects_naive_and_normalizes() -> None:
    col = UTCDateTime()
    with pytest.raises(ValueError, match="timezone offset"):
        col.process_bind_param(datetime(2026, 1, 1, 12, 0), None)  # noqa: DTZ001  # type: ignore[arg-type]
    ist = timezone(timedelta(hours=5, minutes=30))
    stored = col.process_bind_param(datetime(2026, 1, 1, 17, 30, tzinfo=ist), None)  # type: ignore[arg-type]
    assert stored == datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001 - naive UTC in storage
    loaded = col.process_result_value(stored, None)  # type: ignore[arg-type]
    assert loaded == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert loaded is not None and loaded.tzinfo is UTC
