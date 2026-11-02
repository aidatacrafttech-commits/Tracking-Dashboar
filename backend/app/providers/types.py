"""Normalized, provider-independent result types returned by every adapter.

Rules enforced here (not just documented):
  * numbers are Decimal, never float;
  * a missing value is ``None`` with ``source_type=UNAVAILABLE`` and a reason, never 0;
  * timestamps are timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.models.enums import HealthSource, HealthStatus, PaymentKind


class Operation(StrEnum):
    BALANCE = "balance"
    USAGE = "usage"
    BILLING = "billing"
    HEALTH = "health"
    RESOURCES = "resources"


class SourceType(StrEnum):
    API_VERIFIED = "api_verified"
    USAGE_ESTIMATE = "usage_estimate"
    MANUAL = "manual"
    UNAVAILABLE = "unavailable"


class DataLatency(StrEnum):
    REALTIME = "realtime"  # reflects the provider's state at request time
    DELAYED = "delayed"  # billing pipeline data that can lag (AWS Cost Explorer, credits)


class SyncStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # some sub-requests failed; partial data recorded
    ERROR = "error"
    UNSUPPORTED = "unsupported"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"


class ErrorCode(StrEnum):
    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    UPSTREAM_ERROR = "upstream_error"
    INVALID_REQUEST = "invalid_request"
    MALFORMED_RESPONSE = "malformed_response"
    DATA_UNAVAILABLE = "data_unavailable"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    UNEXPECTED = "unexpected"


# Metric types stored as balance snapshots; everything else is a usage/cost series.
class MetricType(StrEnum):
    PREPAID_BALANCE = "prepaid_balance"
    PLAN_QUOTA_REMAINING = "plan_quota_remaining"
    PROMOTIONAL_CREDIT_REMAINING = "promotional_credit_remaining"
    COST = "cost"
    COST_MONTH = "cost_month"
    COST_FORECAST_MONTH = "cost_forecast_month"
    OVERAGE = "overage"
    OPEN_INVOICE_AMOUNT = "open_invoice_amount"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    REQUESTS = "requests"
    AUDIO_HOURS = "audio_hours"
    TTS_CHARACTERS = "tts_characters"
    CREDITS_USED = "credits_used"
    S3_BUCKET_SIZE_GB = "s3_bucket_size_gb"
    S3_OBJECT_COUNT = "s3_object_count"


BALANCE_METRICS = frozenset(
    {
        MetricType.PREPAID_BALANCE,
        MetricType.PLAN_QUOTA_REMAINING,
        MetricType.PROMOTIONAL_CREDIT_REMAINING,
    }
)


@dataclass(frozen=True, slots=True)
class ProviderError:
    code: ErrorCode
    # Safe for logs, DB and API: never contains response bodies, headers or secrets.
    message: str
    retryable: bool = False
    http_status: int | None = None


class ProviderOperationError(Exception):
    def __init__(self, error: ProviderError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


class MalformedResponseError(ProviderOperationError):
    def __init__(self, what: str) -> None:
        super().__init__(
            ProviderError(ErrorCode.MALFORMED_RESPONSE, f"unexpected response: {what}")
        )


@dataclass(frozen=True, slots=True)
class Metric:
    provider: str
    metric_type: MetricType
    value: Decimal | None
    unit: str  # ISO currency code or a unit such as "tokens", "hours", "CREDITS", "Bytes"
    source_type: SourceType
    observed_at: datetime
    period_start: datetime | None = None
    period_end: datetime | None = None
    dimension: str = ""  # e.g. model or AWS service; "" = provider total
    data_as_of: datetime | None = None
    latency: DataLatency = DataLatency.REALTIME
    is_final: bool = False
    unavailable_reason: str | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, float):
            raise TypeError("Metric.value must be Decimal, not float")
        if self.value is None and (
            self.source_type is not SourceType.UNAVAILABLE or not self.unavailable_reason
        ):
            raise ValueError("a missing value must be UNAVAILABLE with a reason")
        if self.value is not None and self.source_type is SourceType.UNAVAILABLE:
            raise ValueError("UNAVAILABLE metrics cannot carry a value")
        for stamp in (self.observed_at, self.period_start, self.period_end, self.data_as_of):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("Metric timestamps must be timezone-aware")
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end must be given together")
        if self.period_start and self.period_end and self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")


@dataclass(frozen=True, slots=True)
class ProviderPayment:
    """A payment/top-up the provider itself reports (stored as provider_verified)."""

    external_id: str
    amount: Decimal
    currency: str
    transaction_date: datetime
    kind: PaymentKind = PaymentKind.TOP_UP
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    resource_type: str  # ec2_instance | ebs_volume
    resource_id: str
    region: str
    name: str | None
    state: str | None
    health_status: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    source: HealthSource
    indicator: str | None = None
    description: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    provider: str
    operation: Operation
    status: SyncStatus
    observed_at: datetime
    metrics: tuple[Metric, ...] = ()
    payments: tuple[ProviderPayment, ...] = ()
    resources: tuple[ResourceRecord, ...] = ()
    health: HealthResult | None = None
    error: ProviderError | None = None
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status in (SyncStatus.OK, SyncStatus.PARTIAL)
