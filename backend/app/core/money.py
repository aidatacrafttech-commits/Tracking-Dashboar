"""Exact decimal handling for money and quantities.

Values are stored as integers scaled by 10**6 (micro-units) so SQLite never sees
floats. Six decimal places cover fractional-cent API pricing (e.g. per-token costs).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

STORAGE_SCALE = 6
_QUANT = Decimal(1).scaleb(-STORAGE_SCALE)
# Comfortably inside SQLite's signed 64-bit INTEGER after scaling.
MAX_ABS_VALUE = Decimal("1000000000000")  # 1e12

# ISO 4217 minor units. Every currency we currently allow uses 2.
CURRENCY_MINOR_UNITS: dict[str, int] = {"USD": 2, "INR": 2, "EUR": 2, "GBP": 2, "PLN": 2}


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def check_decimal(
    value: Decimal,
    *,
    max_places: int = STORAGE_SCALE,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    """Validate a Decimal for storage; raises ValueError with a user-facing message."""
    if not value.is_finite():
        raise ValueError("must be a finite number")
    if abs(value) >= MAX_ABS_VALUE:
        raise ValueError(f"must be smaller than {MAX_ABS_VALUE:,}")
    if not allow_negative and value < 0:
        raise ValueError("must not be negative")
    if not allow_zero and value == 0:
        raise ValueError("must be greater than zero")
    if decimal_places(value) > max_places:
        raise ValueError(f"must have at most {max_places} decimal places")
    return value


def quantize_for_storage(value: Decimal) -> Decimal:
    """Round provider-reported values to the storage scale (banker's rounding).

    Providers return floats like 1619.7242069444444 audio hours; one millionth of a unit
    is far below any meaningful resolution. Owner-entered values are validated instead.
    """
    return value.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


def minor_units_for(currency: str) -> int:
    return CURRENCY_MINOR_UNITS.get(currency, 2)


def to_storage_int(value: Decimal) -> int:
    try:
        check_decimal(value, allow_negative=True)
        return int(value.quantize(_QUANT).scaleb(STORAGE_SCALE))
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError("invalid decimal value") from exc


def from_storage_int(value: int) -> Decimal:
    return strip_trailing_zeros(Decimal(value).scaleb(-STORAGE_SCALE))


def strip_trailing_zeros(value: Decimal) -> Decimal:
    """Drop insignificant zeros without switching to exponent notation (100, not 1E+2)."""
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return normalized.quantize(Decimal(1))
    return normalized
