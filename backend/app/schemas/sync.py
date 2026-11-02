from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.core.money import check_decimal
from app.models.enums import BalanceKind, HealthSource, HealthStatus, RunStatus
from app.providers.types import Operation, SyncStatus
from app.schemas.common import ApiModel, InputModel, UTCInput


class CapabilityRead(ApiModel):
    operation: Operation
    supported: bool
    note: str


class IntegrationRead(ApiModel):
    # ok = configured; not_configured lists the env variable NAMES needed (never values)
    status: SyncStatus
    missing_config: list[str]
    note: str | None
    capabilities: list[CapabilityRead]


class SyncStateRead(ApiModel):
    operation: Operation
    supported: bool
    status: str | None  # last outcome; null = never attempted
    last_attempt_at: datetime | None
    last_successful_sync: datetime | None
    seconds_since_success: int | None
    is_stale: bool
    consecutive_failures: int
    error_code: str | None
    error_message: str | None


class ProviderSyncRead(ApiModel):
    provider: str
    running: bool
    integration: IntegrationRead
    operations: list[SyncStateRead]


class OperationOutcome(ApiModel):
    operation: Operation
    status: SyncStatus
    error_code: str | None
    error_message: str | None
    warnings: list[str]
    records: int | None = None


class SyncRunRead(ApiModel):
    provider: str
    skipped: bool
    reason: str | None
    run_id: int | None
    status: RunStatus | None
    records_written: int
    operations: list[OperationOutcome]


class ManualBalanceCreate(InputModel):
    """Owner-entered balance, e.g. from the OpenAI or Sarvam dashboard."""

    amount: Decimal = Field(description="Balance as a decimal string; may be negative")
    unit: str = Field(min_length=3, max_length=16, description="Must match the provider's unit")
    balance_kind: BalanceKind | None = None
    observed_at: UTCInput | None = Field(
        default=None, description="When the balance was read in the provider dashboard"
    )
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError('send amounts as strings, e.g. "42.10"')
        return value

    @field_validator("amount")
    @classmethod
    def _amount(cls, value: Decimal) -> Decimal:
        # Prepaid balances can be slightly negative (OpenAI documents delayed cut-off).
        return check_decimal(value, allow_negative=True)

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        return value.strip().upper()


class HealthCheckRead(ApiModel):
    id: int
    source: HealthSource
    status: HealthStatus
    indicator: str | None
    description: str | None
    http_status: int | None
    latency_ms: int | None
    error_message: str | None
    checked_at: datetime


class ResourceRead(ApiModel):
    resource_type: str
    resource_id: str
    region: str
    name: str | None
    state: str | None
    health_status: str | None
    details: dict[str, Any] | None
    observed_at: datetime


class ResourceList(ApiModel):
    items: list[ResourceRead]
    observed_at: datetime | None
    is_stale: bool
