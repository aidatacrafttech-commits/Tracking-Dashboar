"""Usage-based balance estimate for providers with a cost API but no balance API (OpenAI).

estimate = last manual balance
         - provider-reported cost for every UTC day starting with the entry's day

Payments are deliberately NOT added: a recorded payment is not proof that the provider
balance increased. A top-up only counts after the owner reconciles it (or enters a new
balance), which creates a new manual baseline.

Costs come in whole-day buckets, so spend from earlier on the entry day is subtracted
too. That makes the estimate conservative (it can only read low, never high), which is
the safe direction for low-balance alerts.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.session import transaction
from app.models import Provider, ProviderBalanceSnapshot, ProviderUsageSnapshot
from app.models.enums import BalanceKind, ValueSource
from app.providers.types import MetricType

ESTIMATE_METHOD = "manual_balance_minus_daily_costs"


def _day_floor(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


class BalanceEstimator:
    def __init__(self, session: Session) -> None:
        self.session = session

    def estimate(self, provider: Provider) -> ProviderBalanceSnapshot | None:
        """Write a usage_estimate snapshot if a manual baseline exists; else return None."""
        with transaction(self.session):
            base = self.session.scalar(
                select(ProviderBalanceSnapshot)
                .where(
                    ProviderBalanceSnapshot.provider_id == provider.id,
                    ProviderBalanceSnapshot.balance_kind == BalanceKind.PREPAID_BALANCE,
                    ProviderBalanceSnapshot.source == ValueSource.MANUAL,
                    ProviderBalanceSnapshot.amount.is_not(None),
                )
                .order_by(
                    ProviderBalanceSnapshot.observed_at.desc(), ProviderBalanceSnapshot.id.desc()
                )
                .limit(1)
            )
            if base is None or base.amount is None:
                return None
            since = base.observed_at
            currency = base.unit

            costs = list(
                self.session.scalars(
                    select(ProviderUsageSnapshot).where(
                        ProviderUsageSnapshot.provider_id == provider.id,
                        ProviderUsageSnapshot.metric == MetricType.COST.value,
                        ProviderUsageSnapshot.dimension == "",
                        ProviderUsageSnapshot.cost_currency == currency,
                        ProviderUsageSnapshot.period_start >= _day_floor(since),
                    )
                )
            )
            cost_total = sum((c.cost_amount or Decimal(0) for c in costs), Decimal(0))
            latest_cost_data = max((c.observed_at for c in costs), default=None)

            snapshot = ProviderBalanceSnapshot(
                provider_id=provider.id,
                balance_kind=BalanceKind.PREPAID_BALANCE,
                amount=base.amount - cost_total,
                unit=currency,
                unit_type=base.unit_type,
                source=ValueSource.USAGE_ESTIMATE,
                observed_at=utcnow(),
                data_as_of=latest_cost_data,
                details={
                    "method": ESTIMATE_METHOD,
                    "base_snapshot_id": base.id,
                    "base_amount": str(base.amount),
                    "base_observed_at": since.isoformat(),
                    "costs_since": str(cost_total),
                    "cost_days_counted": len(costs),
                    "conservative": True,
                },
            )
            self.session.add(snapshot)
            self.session.flush()
            return snapshot
