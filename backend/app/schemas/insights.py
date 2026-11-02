from __future__ import annotations

from datetime import datetime

from app.models.enums import AlertState, BalanceCapability, EvaluationStatus
from app.providers.types import SourceType, SyncStatus
from app.schemas.alerts import AlertEventRead
from app.schemas.common import ApiModel, DecimalStr
from app.schemas.payment import PaymentRead
from app.schemas.provider import ThresholdHeadroom


class SeriesPoint(ApiModel):
    period_start: datetime
    period_end: datetime
    value: DecimalStr
    is_final: bool


class Series(ApiModel):
    metric: str
    unit: str
    dimension: str  # "" = provider total
    provider: str | None = None
    points: list[SeriesPoint]


class MetricTotal(ApiModel):
    metric: str
    unit: str
    value: DecimalStr
    dimension: str = ""
    observed_at: datetime | None = None
    is_final: bool | None = None
    period_start: datetime | None = None  # set for point-in-time metrics (e.g. which month)


class UsageSeriesRead(ApiModel):
    provider: str
    days: int
    since: datetime
    series: list[Series]
    # Sums of daily metrics since the start of the current UTC month, per unit.
    month_to_date: list[MetricTotal]
    # Latest value of point-in-time metrics (monthly cost, forecast, overage, S3 size...).
    latest: list[MetricTotal]


class OverviewProvider(ApiModel):
    id: str
    display_name: str
    # api | partial | manual_only | postpaid: tells the UI whether a balance can be fetched at all
    balance_capability: BalanceCapability | None = None
    integration_status: SyncStatus | None
    alert_state: AlertState | None
    evaluation_status: EvaluationStatus | None
    balance: DecimalStr | None
    balance_unit: str | None
    balance_source_type: SourceType | None
    balance_observed_at: datetime | None
    balance_is_stale: bool | None
    threshold: DecimalStr | None
    threshold_unit: str | None
    headroom: ThresholdHeadroom | None
    last_successful_sync: datetime | None
    failing_operations: list[str]
    last_error_code: str | None
    # Provider-reported spend since the 1st of this month (UTC), one entry per currency,
    # summed on the server. Empty when the provider reports no costs.
    month_to_date_cost: list[MetricTotal] = []
    # This month's headline usage (e.g. ElevenLabs credits) for providers that report usage
    # but not money. Never converted to money.
    month_to_date_usage: list[MetricTotal] = []
    # Whether the integration can report usage or costs at all (False: e.g. Sarvam).
    spend_reported: bool | None = None


class OverviewCounts(ApiModel):
    providers: int
    low_balance: int
    integration_failures: int
    not_configured: int
    stale_balances: int


class OverviewRead(ApiModel):
    generated_at: datetime
    counts: OverviewCounts
    last_successful_sync: datetime | None
    providers: list[OverviewProvider]
    # Separate entries per currency; never summed across currencies.
    aws_month_to_date: list[MetricTotal]
    aws_month_forecast: list[MetricTotal]
    recent_payments: list[PaymentRead]
    recent_alerts: list[AlertEventRead]
    spend_trend: list[Series]
    usage_trend: list[Series]
