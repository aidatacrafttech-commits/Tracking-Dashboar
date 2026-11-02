from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from app.core.money import check_decimal
from app.models.enums import AlertState, BalanceKind, EvaluationStatus, UnitType
from app.schemas.common import EMAIL_PATTERN, ApiModel, DecimalStr, InputModel

CREDIT_UNITS = frozenset({"CREDITS"})
EligibleSource = Literal["api_verified", "manual", "usage_estimate"]


class ThresholdUpsert(InputModel):
    """Full replacement of a provider's threshold (PUT semantics)."""

    amount: Decimal = Field(description="Alert when balance <= amount. Decimal string.")
    unit: str = Field(
        min_length=3, max_length=16, description="Currency code or CREDITS; must match provider"
    )
    balance_kind: BalanceKind | None = Field(
        default=None, description="Which balance to watch; defaults per provider"
    )
    is_enabled: bool = True
    notify_on_breach: bool = True
    notify_on_recovery: bool = True
    recovery_margin: Decimal = Field(
        default=Decimal(0),
        description="Recover only when balance > amount + margin (prevents flapping)",
    )
    cooldown_minutes: int | None = Field(
        default=None,
        ge=15,
        le=10080,
        description="Repeat the low-balance email at most this often while still breached; "
        "null = one email per breach (next one only after recovery and a new crossing)",
    )
    notification_emails: list[str] | None = Field(
        default=None, max_length=10, description="null = ALERT_RECIPIENTS from the environment"
    )
    eligible_sources: list[EligibleSource] = Field(
        default_factory=lambda: ["api_verified", "manual"],
        min_length=1,
        description="Balance sources allowed to trigger alerts",
    )
    monitoring_interval_minutes: int | None = Field(
        default=None, ge=5, le=1440, description="Balance polling interval; null = global default"
    )

    @field_validator("eligible_sources")
    @classmethod
    def _dedupe_sources(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("amount", "recovery_margin", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError('send amounts as strings, e.g. "10.00"')
        return value

    @field_validator("amount", "recovery_margin")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        # Currency-specific precision is enforced in ThresholdService once the unit is known.
        return check_decimal(value)

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("notification_emails")
    @classmethod
    def _emails(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            email = raw.strip().lower()
            if len(email) > 254 or not EMAIL_PATTERN.match(email):
                raise ValueError(f"invalid email address: {raw!r}")
            if email not in cleaned:
                cleaned.append(email)
        if not cleaned:
            raise ValueError("use null for default recipients instead of an empty list")
        return cleaned

    @property
    def unit_type(self) -> UnitType:
        return UnitType.CREDITS if self.unit in CREDIT_UNITS else UnitType.CURRENCY


class ThresholdRead(ApiModel):
    provider: str
    balance_kind: BalanceKind
    amount: DecimalStr
    unit: str
    unit_type: UnitType
    is_enabled: bool
    notify_on_breach: bool
    notify_on_recovery: bool
    recovery_margin: DecimalStr
    recovery_boundary: DecimalStr
    cooldown_minutes: int | None
    notification_emails: list[str] | None
    effective_recipients: list[str]
    eligible_sources: list[str]
    monitoring_interval_minutes: int | None
    alert_state: AlertState
    breach_episode: int
    state_changed_at: datetime | None
    last_evaluated_at: datetime | None
    last_evaluation_status: EvaluationStatus | None
    last_evaluation_message: str | None
    last_notified_at: datetime | None
    email_delivery_enabled: bool
    version: int
    updated_at: datetime


class ThresholdSummary(ApiModel):
    amount: DecimalStr
    unit: str
    is_enabled: bool
    alert_state: AlertState
    last_evaluation_status: EvaluationStatus | None = None
