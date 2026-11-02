from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, ValidationInfo, field_validator

from app.core.money import check_decimal, minor_units_for
from app.models.enums import EntryMethod, PaymentKind
from app.schemas.common import ApiModel, DecimalStr, InputModel, UTCInput, normalize_currency


class PaymentCreate(InputModel):
    # currency is declared first so the amount validator can see it.
    currency: str = Field(min_length=3, max_length=3, examples=["USD"])
    amount: Decimal = Field(description='Positive amount as a decimal string, e.g. "50.00"')
    transaction_date: UTCInput = Field(description="ISO-8601 with timezone offset")
    kind: PaymentKind = PaymentKind.TOP_UP
    payment_reference: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        # JSON numbers arrive as float and may already have lost precision.
        if isinstance(value, float):
            raise ValueError('send monetary amounts as strings, e.g. "12.50"')
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return normalize_currency(value)

    @field_validator("payment_reference")
    @classmethod
    def _reference(cls, value: str | None) -> str | None:
        if value is not None and not value.isprintable():
            raise ValueError("must contain printable characters only")
        return value

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        currency = info.data.get("currency")  # absent if currency itself failed validation
        max_places = minor_units_for(currency) if currency else 2
        return check_decimal(value, max_places=max_places, allow_zero=False)


class PaymentRead(ApiModel):
    id: int
    provider: str
    kind: PaymentKind
    amount: DecimalStr
    currency: str
    transaction_date: datetime
    payment_reference: str | None
    external_id: str | None
    entry_method: EntryMethod
    verified_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime | None = None
    reconciled_at: datetime | None = None
    reconciled_balance_snapshot_id: int | None = None
    # Only owner-entered (manual) payments can be edited or deleted.
    editable: bool = False


class CurrencyTotal(ApiModel):
    currency: str
    # Net of refunds within the filtered range.
    net_amount: DecimalStr
    count: int


class PaymentPage(ApiModel):
    items: list[PaymentRead]
    total: int
    limit: int
    offset: int
    totals: list[CurrencyTotal]


class ReconcileBase(ApiModel):
    snapshot_id: int
    amount: DecimalStr
    unit: str
    source: str
    observed_at: datetime


class ReconcilePreview(ApiModel):
    """What an explicit reconciliation would do. Nothing is changed by previewing."""

    payment_id: int
    allowed: bool
    reason: str | None
    base: ReconcileBase | None
    payment_effect: DecimalStr | None  # +amount for top-ups, -amount for refunds
    resulting_balance: DecimalStr | None
    unit: str | None


class ReconcileRequest(InputModel):
    # Echo of the base snapshot the owner reviewed; refused if the balance changed since.
    base_snapshot_id: int = Field(ge=1)
