from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer

from app.core.money import strip_trailing_zeros
from app.core.time import ensure_utc

PROVIDER_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
# Pragmatic address check; delivery is the real test. Avoids an extra dependency.
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def _to_utc(value: datetime) -> datetime:
    return ensure_utc(value)


# Input: aware datetime required (naive rejected), normalised to UTC.
UTCInput = Annotated[datetime, AfterValidator(_to_utc)]

# Output: decimals as JSON strings so no client ever parses money as a float.
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: format(strip_trailing_zeros(v), "f"), return_type=str, when_used="json"
    ),
]


def normalize_currency(value: str) -> str:
    code = value.strip().upper()
    if not CURRENCY_PATTERN.match(code):
        raise ValueError("must be a 3-letter ISO 4217 currency code")
    return code


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InputModel(BaseModel):
    """Request bodies: unknown fields are rejected, strings trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
