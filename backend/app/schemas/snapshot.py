from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import computed_field

from app.models.enums import BalanceKind, UnitType, ValueSource
from app.providers.types import DataLatency, SourceType
from app.schemas.common import ApiModel, DecimalStr


def source_type_for(source: ValueSource, has_value: bool) -> SourceType:
    """Map stored provenance to the normalized source_type shown to clients."""
    if not has_value:
        return SourceType.UNAVAILABLE
    return {
        ValueSource.PROVIDER_API: SourceType.API_VERIFIED,
        ValueSource.BILLING_REPORT: SourceType.API_VERIFIED,
        ValueSource.USAGE_ESTIMATE: SourceType.USAGE_ESTIMATE,
        ValueSource.MANUAL: SourceType.MANUAL,
    }[source]


def latency_for(source: ValueSource) -> DataLatency:
    return DataLatency.DELAYED if source is ValueSource.BILLING_REPORT else DataLatency.REALTIME


class Freshness(ApiModel):
    observed_at: datetime
    data_as_of: datetime | None
    age_seconds: int
    is_stale: bool


class BalanceSnapshotRead(ApiModel):
    id: int
    balance_kind: BalanceKind
    # null means the provider could not supply a value (see unavailable_reason); never 0.
    amount: DecimalStr | None
    unit: str
    unit_type: UnitType
    source: ValueSource
    observed_at: datetime
    data_as_of: datetime | None
    unavailable_reason: str | None
    monitoring_run_id: int | None
    details: dict[str, Any] | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_type(self) -> SourceType:
        return source_type_for(self.source, self.amount is not None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def data_latency(self) -> DataLatency:
        return latency_for(self.source)


class UsageSnapshotRead(ApiModel):
    id: int
    metric: str
    dimension: str
    period_start: datetime
    period_end: datetime
    quantity: DecimalStr | None
    quantity_unit: str | None
    cost_amount: DecimalStr | None
    cost_currency: str | None
    source: ValueSource
    is_final: bool
    observed_at: datetime
    data_as_of: datetime | None
    unavailable_reason: str | None
    monitoring_run_id: int | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_type(self) -> SourceType:
        return source_type_for(
            self.source, self.quantity is not None or self.cost_amount is not None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def data_latency(self) -> DataLatency:
        return latency_for(self.source)


class BalanceSnapshotPage(ApiModel):
    kind: Literal["balance"] = "balance"
    items: list[BalanceSnapshotRead]
    total: int
    limit: int
    offset: int


class UsageSnapshotPage(ApiModel):
    kind: Literal["usage"] = "usage"
    items: list[UsageSnapshotRead]
    total: int
    limit: int
    offset: int
