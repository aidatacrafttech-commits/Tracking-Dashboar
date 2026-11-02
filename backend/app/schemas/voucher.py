"""Voucher management API models.

Two categories share one model: Course vouchers apply to subjects, Live Tutor vouchers apply to
packages (Beginner / Intermediate / Pro). The category comes from the URL
(/vouchers/courses or /vouchers/live-tutor) and never changes after creation.

Editable fields (accepted in requests): code, name, description, discount_type,
discount_value, currency, applies_to_all, subjects | packages, min_purchase_amount,
benefit_validity_days, max_uses (total redemption limit), max_uses_per_user,
max_unique_users, valid_from, valid_until, is_active. Everything else (ids, counters, lock
state, who/when metadata, version) is system-managed and only ever appears in responses.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.models.enums import (
    TutorPackage,
    VoucherActor,
    VoucherCategory,
    VoucherDiscountType,
    VoucherEventOutcome,
    VoucherEventSource,
)
from app.schemas.common import ApiModel, DecimalStr, InputModel, UTCInput, normalize_currency

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,39}$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,127}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_AMOUNT = Decimal("100000000")  # 10 crore: far above any real order
MAX_SUBJECTS = 50

VoucherStatus = Literal["active", "inactive", "scheduled", "expired", "exhausted", "locked"]
FailureReason = Literal[
    "not_found",
    "user_not_registered",
    "locked",
    "inactive",
    "not_started",
    "expired",
    "exhausted",
    "per_user_limit",
    "unique_user_limit",
    "wrong_category",
    "not_applicable",
    "below_minimum",
    "currency_mismatch",
    "order_already_redeemed",
]


def normalize_code(value: str) -> str:
    code = value.strip().upper()
    if not CODE_RE.match(code):
        raise ValueError(
            "3-40 characters: letters, digits, '-' or '_', starting with a letter or digit"
        )
    return code


def normalize_subject(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > 80 or CONTROL_RE.search(name):
        raise ValueError("subject names are 1-80 characters")
    return name


def parse_amount(value: Any, *, places: int = 2) -> Decimal:
    """Money and percentages arrive as strings (or ints), never floats, so nothing is
    rounded by a JSON parser. At most `places` decimal places; never negative."""
    if isinstance(value, float):
        raise ValueError('send amounts as strings, e.g. "499.00"')
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("must be a number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("must be zero or more")
    if amount > MAX_AMOUNT:
        raise ValueError(f"must be at most {MAX_AMOUNT}")
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places:
        raise ValueError(f"at most {places} decimal places")
    return amount


class _VoucherFields(InputModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    discount_type: VoucherDiscountType
    discount_value: Decimal
    currency: str = Field(default="INR", min_length=3, max_length=3)
    # True = every subject / package, including ones added later. Otherwise list them.
    applies_to_all: bool = False
    subjects: list[str] = Field(default_factory=list, max_length=MAX_SUBJECTS)
    packages: list[TutorPackage] = Field(default_factory=list, max_length=3)
    min_purchase_amount: Decimal | None = None
    benefit_validity_days: int | None = Field(default=None, ge=1, le=3650)
    max_uses: int | None = Field(default=None, ge=1, le=10_000_000)
    max_uses_per_user: int | None = Field(default=None, ge=1, le=1_000_000)
    max_unique_users: int | None = Field(default=None, ge=1, le=10_000_000)
    valid_from: UTCInput | None = None
    valid_until: UTCInput | None = None
    is_active: bool = True

    @field_validator("discount_value", mode="before")
    @classmethod
    def _discount_value(cls, value: Any) -> Decimal:
        return parse_amount(value)

    @field_validator("min_purchase_amount", mode="before")
    @classmethod
    def _min_purchase(cls, value: Any) -> Decimal | None:
        return None if value is None or value == "" else parse_amount(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return normalize_currency(value)

    @field_validator("subjects")
    @classmethod
    def _subjects(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = normalize_subject(raw)
            if name.casefold() not in seen:
                seen.add(name.casefold())
                out.append(name)
        return out

    @field_validator("packages")
    @classmethod
    def _packages(cls, value: list[TutorPackage]) -> list[TutorPackage]:
        order = list(TutorPackage)
        return sorted(set(value), key=order.index)

    @model_validator(mode="after")
    def _rules(self) -> _VoucherFields:
        if self.discount_value <= 0:
            raise ValueError("discount_value must be more than 0")
        if self.discount_type is VoucherDiscountType.PERCENTAGE and self.discount_value > 100:
            raise ValueError("a percentage discount cannot be more than 100")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        if self.max_uses and self.max_uses_per_user and self.max_uses_per_user > self.max_uses:
            raise ValueError("max_uses_per_user cannot exceed max_uses")
        if self.max_uses and self.max_unique_users and self.max_unique_users > self.max_uses:
            raise ValueError("max_unique_users cannot exceed max_uses")
        return self


class VoucherCreate(_VoucherFields):
    # Optional: generated (VCH-XXXXXXXX) when omitted.
    code: str | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return normalize_code(value) if value else None


class VoucherUpdate(_VoucherFields):
    code: str
    # The version the edit was based on; a stale version is refused (409).
    version: int = Field(ge=1)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return normalize_code(value)


class LockRequest(InputModel):
    reason: str | None = Field(default=None, max_length=255)


class VoucherRead(ApiModel):
    id: int
    category: VoucherCategory
    code: str
    name: str
    description: str | None
    discount_type: VoucherDiscountType
    discount_value: DecimalStr
    currency: str
    applies_to_all: bool
    subjects: list[str]  # Course vouchers (empty when applies_to_all)
    packages: list[TutorPackage]  # Live Tutor vouchers (empty when applies_to_all)
    min_purchase_amount: DecimalStr | None
    benefit_validity_days: int | None
    max_uses: int | None
    max_uses_per_user: int | None
    max_unique_users: int | None
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    # system-managed
    status: VoucherStatus
    use_count: int
    remaining_uses: int | None
    unique_user_count: int
    remaining_unique_users: int | None
    last_used_at: datetime | None
    is_locked: bool
    lock_reason: str | None
    locked_at: datetime | None
    locked_by: VoucherActor | None
    unlocked_at: datetime | None
    unlocked_by: VoucherActor | None
    created_at: datetime
    created_by: VoucherActor
    updated_at: datetime
    updated_by: VoucherActor
    version: int
    can_delete: bool


class VoucherPage(ApiModel):
    items: list[VoucherRead]
    total: int
    limit: int
    offset: int


class SubjectList(ApiModel):
    items: list[str]


# --- users and capacity ------------------------------------------------------------------


class VoucherUserCreate(InputModel):
    external_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, value: str) -> str:
        if not EXTERNAL_ID_RE.match(value):
            raise ValueError("letters, digits and . _ : @ - only (max 128)")
        return value


class VoucherUserRead(ApiModel):
    id: int
    external_id: str
    display_name: str | None
    created_at: datetime
    created_by: VoucherActor
    redemptions: int = 0


class VoucherUserPage(ApiModel):
    items: list[VoucherUserRead]
    total: int
    limit: int
    offset: int


class Capacity(ApiModel):
    user_limit: int
    total_users: int
    remaining: int
    percent_used: float
    default_limit: int


class UserLimitUpdate(InputModel):
    user_limit: int = Field(ge=1, le=10_000_000)


# --- validation, redemption and usage ------------------------------------------------------


class VoucherCheckRequest(InputModel):
    """What the user is buying. `item` is a subject name (course) or a package id
    (live_tutor: beginner, intermediate or pro)."""

    code: str = Field(min_length=1, max_length=40)
    user_external_id: str = Field(min_length=1, max_length=128)
    category: VoucherCategory
    item: str = Field(min_length=1, max_length=80)
    order_amount: Decimal
    currency: str = Field(default="INR", min_length=3, max_length=3)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("order_amount", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return parse_amount(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return normalize_currency(value)

    @model_validator(mode="after")
    def _item(self) -> VoucherCheckRequest:
        if self.category is VoucherCategory.LIVE_TUTOR:
            item = self.item.strip().lower()
            if item not in {p.value for p in TutorPackage}:
                raise ValueError("item must be beginner, intermediate or pro")
            self.item = item
        else:
            self.item = normalize_subject(self.item)
        return self


class RedeemRequest(VoucherCheckRequest):
    # The course order or Live Tutor booking this redemption belongs to. One order can use
    # a voucher once (retrying the same order is refused, not double-counted).
    order_reference: str = Field(min_length=1, max_length=128)

    @field_validator("order_reference")
    @classmethod
    def _reference(cls, value: str) -> str:
        if not REFERENCE_RE.match(value):
            raise ValueError("letters, digits and . _ : / # @ - only (max 128)")
        return value


class CheckResult(ApiModel):
    valid: bool
    reason: FailureReason | None
    message: str
    code: str
    category: VoucherCategory
    voucher_id: int | None
    discount_type: VoucherDiscountType | None
    discount_value: DecimalStr | None
    currency: str
    order_amount: DecimalStr
    discount_amount: DecimalStr  # 0 when not valid
    final_amount: DecimalStr
    benefit_expires_at: datetime | None  # if redeemed now


class RedeemResult(ApiModel):
    event_id: int
    voucher_id: int
    code: str
    category: VoucherCategory
    user_external_id: str
    item: str
    order_reference: str
    currency: str
    order_amount: DecimalStr
    discount_amount: DecimalStr
    final_amount: DecimalStr
    benefit_expires_at: datetime | None
    use_count: int
    remaining_uses: int | None
    unique_user_count: int
    occurred_at: datetime


class UsageEventRead(ApiModel):
    id: int
    voucher_id: int | None
    voucher_code: str
    category: VoucherCategory | None
    user_external_id: str | None
    item: str | None
    order_reference: str | None
    order_amount: DecimalStr | None
    discount_amount: DecimalStr | None
    currency: str | None
    benefit_expires_at: datetime | None
    outcome: VoucherEventOutcome
    failure_reason: str | None
    source: VoucherEventSource
    actor: VoucherActor
    occurred_at: datetime


class UsageEventPage(ApiModel):
    items: list[UsageEventRead]
    total: int
    limit: int
    offset: int


# --- summary and analytics -----------------------------------------------------------------


class MoneyTotal(ApiModel):
    currency: str
    amount: DecimalStr


class VoucherSummary(ApiModel):
    category: VoucherCategory | None  # None = both categories
    total_vouchers: int
    active: int
    scheduled: int
    expired: int
    inactive: int
    exhausted: int
    locked: int
    unlocked: int
    total_redemptions: int  # all-time successful
    failed_redemptions: int
    unique_users_reached: int  # distinct users with a successful redemption
    redemptions_today: int
    redemptions_this_week: int
    redemptions_this_month: int
    discount_given: list[MoneyTotal]  # all-time, per currency
    total_users: int  # registered voucher holders (shared by both categories)
    user_limit: int
    remaining_capacity: int


class UsageBucket(ApiModel):
    start: datetime  # bucket start (UTC instant of local midnight / week / month start)
    end: datetime
    label: str
    success: int
    failed: int


class VoucherUsageRow(ApiModel):
    voucher_id: int
    code: str
    name: str
    category: VoucherCategory
    status: VoucherStatus
    use_count: int  # all-time
    in_range: int  # successful uses inside the selected range
    last_used_at: datetime | None


class AnalyticsRead(ApiModel):
    granularity: Literal["day", "week", "month"]
    timezone: str
    start: datetime
    end: datetime
    category: VoucherCategory | None
    voucher_id: int | None
    summary: VoucherSummary
    series: list[UsageBucket]
    range_success: int
    range_failed: int
    per_voucher: list[VoucherUsageRow]
    most_used: list[VoucherUsageRow]
    least_used: list[VoucherUsageRow]


# --- audit -------------------------------------------------------------------------------------


class AuditEntryRead(ApiModel):
    id: int
    occurred_at: datetime
    actor: str
    via: str | None
    action: str
    entity_type: str
    entity_id: str | None
    changes: dict[str, Any] | None


class AuditPage(ApiModel):
    items: list[AuditEntryRead]
    total: int
    limit: int
    offset: int
