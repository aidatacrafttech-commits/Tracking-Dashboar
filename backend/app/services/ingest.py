"""Persists normalized adapter results. Synchronous; called from worker threads.

Everything from one OperationResult (data rows + sync state) is written in a single
transaction. A failed operation writes no data rows, so the previous good snapshot stays
the latest one and the sync state records the error and failure count.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.money import MAX_ABS_VALUE, quantize_for_storage
from app.core.time import utcnow
from app.db.session import transaction
from app.models import (
    InfrastructureResourceSnapshot,
    MonitoringRun,
    PaymentTransaction,
    Provider,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ProviderUsageSnapshot,
    ServiceHealthCheck,
)
from app.models.enums import (
    Actor,
    BalanceKind,
    EntryMethod,
    RunStatus,
    RunTrigger,
    UnitType,
    ValueSource,
)
from app.providers.types import (
    BALANCE_METRICS,
    DataLatency,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    SourceType,
    SyncStatus,
)

logger = logging.getLogger(__name__)

_CURRENCY = re.compile(r"^[A-Z]{3}$")

BALANCE_KIND_FOR = {
    MetricType.PREPAID_BALANCE: BalanceKind.PREPAID_BALANCE,
    MetricType.PLAN_QUOTA_REMAINING: BalanceKind.PLAN_QUOTA,
    MetricType.PROMOTIONAL_CREDIT_REMAINING: BalanceKind.PROMOTIONAL_CREDIT,
}


def is_currency(unit: str) -> bool:
    return bool(_CURRENCY.match(unit))


def value_source(metric: Metric) -> ValueSource:
    if metric.source_type is SourceType.MANUAL:
        return ValueSource.MANUAL
    if metric.source_type is SourceType.USAGE_ESTIMATE:
        return ValueSource.USAGE_ESTIMATE
    # api_verified (or unavailable from the API): delayed billing data is labelled as such.
    if metric.latency is DataLatency.DELAYED:
        return ValueSource.BILLING_REPORT
    return ValueSource.PROVIDER_API


class IngestService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- monitoring runs ----------------------------------------------------------------
    def provider(self, slug: str) -> Provider:
        provider = self.session.scalar(select(Provider).where(Provider.slug == slug))
        if provider is None:
            raise LookupError(f"unknown provider {slug!r}")
        return provider

    def start_run(self, slug: str, job_name: str, trigger: RunTrigger) -> int:
        with transaction(self.session):
            run = MonitoringRun(
                provider_id=self.provider(slug).id,
                job_name=job_name,
                trigger=trigger,
                status=RunStatus.RUNNING,
            )
            self.session.add(run)
            self.session.flush()
            return run.id

    def finish_run(
        self, run_id: int, status: RunStatus, records: int, error: str | None = None
    ) -> None:
        with transaction(self.session):
            run = self.session.get(MonitoringRun, run_id)
            if run is None:
                return
            run.status = status
            run.finished_at = utcnow()
            run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
            run.records_written = records
            run.error_message = error[:1000] if error else None

    def recover_interrupted_runs(self) -> int:
        """Mark runs left 'running' by a crash/restart as failed."""
        with transaction(self.session):
            result = self.session.execute(
                update(MonitoringRun)
                .where(MonitoringRun.status == RunStatus.RUNNING)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=utcnow(),
                    error_type="Interrupted",
                    error_message="process stopped before the run finished",
                )
            )
            return result.rowcount or 0

    # --- sync state ---------------------------------------------------------------------
    def sync_states(self, slug: str) -> dict[str, ProviderSyncState]:
        provider = self.provider(slug)
        rows = self.session.scalars(
            select(ProviderSyncState).where(ProviderSyncState.provider_id == provider.id)
        )
        return {row.operation: row for row in rows}

    def _update_sync_state(
        self, provider: Provider, result: OperationResult, run_id: int | None
    ) -> None:
        state = self.session.scalar(
            select(ProviderSyncState).where(
                ProviderSyncState.provider_id == provider.id,
                ProviderSyncState.operation == result.operation.value,
            )
        )
        if state is None:
            state = ProviderSyncState(
                provider_id=provider.id, operation=result.operation.value, consecutive_failures=0
            )
            self.session.add(state)
        state.status = result.status.value
        state.last_attempt_at = result.observed_at
        state.last_run_id = run_id
        if result.succeeded:
            state.last_success_at = result.observed_at
            state.consecutive_failures = 0
            state.last_error_code = None
            state.last_error_message = "; ".join(result.warnings)[:500] or None
        elif result.status is SyncStatus.ERROR:
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
            state.last_error_code = result.error.code.value if result.error else "unexpected"
            state.last_error_message = result.error.message[:500] if result.error else None
        else:  # unsupported / not_configured / disabled: informational, not a failure
            state.consecutive_failures = 0
            state.last_error_code = result.error.code.value if result.error else None
            state.last_error_message = result.error.message[:500] if result.error else None

    # --- data ---------------------------------------------------------------------------
    def record(self, slug: str, run_id: int | None, result: OperationResult) -> int:
        with transaction(self.session):
            provider = self.provider(slug)
            written = 0
            if result.succeeded:
                for metric in result.metrics:
                    written += self._metric(provider, run_id, metric)
                for payment in result.payments:
                    written += self._payment(provider, payment)
                for resource in result.resources:
                    self.session.add(
                        InfrastructureResourceSnapshot(
                            provider_id=provider.id,
                            monitoring_run_id=run_id,
                            resource_type=resource.resource_type,
                            resource_id=resource.resource_id[:128],
                            region=resource.region,
                            name=resource.name[:255] if resource.name else None,
                            state=resource.state,
                            health_status=resource.health_status,
                            details=resource.details,
                            observed_at=result.observed_at,
                        )
                    )
                    written += 1
                if result.health is not None:
                    h = result.health
                    self.session.add(
                        ServiceHealthCheck(
                            provider_id=provider.id,
                            monitoring_run_id=run_id,
                            source=h.source,
                            status=h.status,
                            indicator=h.indicator,
                            description=h.description,
                            http_status=h.http_status,
                            latency_ms=h.latency_ms,
                            checked_at=result.observed_at,
                        )
                    )
                    written += 1
            elif result.operation is Operation.HEALTH and result.status is SyncStatus.ERROR:
                # A failed status check is itself a health data point ("unknown").
                from app.models.enums import HealthSource, HealthStatus

                self.session.add(
                    ServiceHealthCheck(
                        provider_id=provider.id,
                        monitoring_run_id=run_id,
                        source=HealthSource.STATUS_PAGE,
                        status=HealthStatus.UNKNOWN,
                        http_status=result.error.http_status if result.error else None,
                        error_message=result.error.message[:500] if result.error else None,
                        checked_at=result.observed_at,
                    )
                )
                written += 1
            self._update_sync_state(provider, result, run_id)
            return written

    def _metric(self, provider: Provider, run_id: int | None, metric: Metric) -> int:
        source = value_source(metric)
        value = None if metric.value is None else quantize_for_storage(metric.value)
        if value is not None and abs(value) >= MAX_ABS_VALUE:
            logger.warning(
                "metric value out of storable range; skipped",
                extra={"provider": provider.slug, "metric": metric.metric_type.value},
            )
            return 0
        if metric.metric_type in BALANCE_METRICS:
            self.session.add(
                ProviderBalanceSnapshot(
                    provider_id=provider.id,
                    monitoring_run_id=run_id,
                    balance_kind=BALANCE_KIND_FOR[metric.metric_type],
                    amount=value,
                    unit=metric.unit,
                    unit_type=UnitType.CURRENCY if is_currency(metric.unit) else UnitType.CREDITS,
                    source=source,
                    observed_at=metric.observed_at,
                    data_as_of=metric.data_as_of,
                    unavailable_reason=metric.unavailable_reason,
                    details=metric.details,
                )
            )
            return 1

        if metric.period_start is None or metric.period_end is None:
            logger.warning(
                "usage metric without period skipped",
                extra={"provider": provider.slug, "metric": metric.metric_type.value},
            )
            return 0
        currency = is_currency(metric.unit)
        values = {
            "quantity": None if currency else value,
            "quantity_unit": None if currency else metric.unit,
            "cost_amount": value if currency else None,
            "cost_currency": metric.unit if currency else None,
            "is_final": metric.is_final,
            "observed_at": metric.observed_at,
            "data_as_of": metric.data_as_of,
            "unavailable_reason": metric.unavailable_reason,
            "details": metric.details,
            "monitoring_run_id": run_id,
        }
        existing = self.session.scalar(
            select(ProviderUsageSnapshot).where(
                ProviderUsageSnapshot.provider_id == provider.id,
                ProviderUsageSnapshot.metric == metric.metric_type.value,
                ProviderUsageSnapshot.dimension == metric.dimension[:128],
                ProviderUsageSnapshot.period_start == metric.period_start,
                ProviderUsageSnapshot.period_end == metric.period_end,
                ProviderUsageSnapshot.source == source,
            )
        )
        if existing is None:
            self.session.add(
                ProviderUsageSnapshot(
                    provider_id=provider.id,
                    metric=metric.metric_type.value,
                    dimension=metric.dimension[:128],
                    period_start=metric.period_start,
                    period_end=metric.period_end,
                    source=source,
                    **values,
                )
            )
        elif existing.is_final and not metric.is_final:
            return 0  # never downgrade a finalized figure with a provisional one
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        return 1

    def _payment(self, provider: Provider, payment) -> int:  # type: ignore[no-untyped-def]
        existing = self.session.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.provider_id == provider.id,
                PaymentTransaction.external_id == payment.external_id,
            )
        )
        if existing is not None:
            return 0  # provider records are immutable once captured
        if payment.amount <= 0 or not is_currency(payment.currency):
            logger.warning(
                "provider payment skipped: invalid amount or currency",
                extra={"provider": provider.slug},
            )
            return 0
        self.session.add(
            PaymentTransaction(
                provider_id=provider.id,
                kind=payment.kind,
                amount=quantize_for_storage(payment.amount),
                currency=payment.currency,
                transaction_date=payment.transaction_date,
                external_id=payment.external_id[:128],
                entry_method=EntryMethod.PROVIDER_VERIFIED,
                verified_at=utcnow(),
                notes=payment.description[:2000] if payment.description else None,
                created_by=Actor.SYSTEM,
            )
        )
        return 1

    # --- queries for scheduling ---------------------------------------------------------
    def monitoring_interval(self, slug: str) -> int | None:
        from app.models import ThresholdConfig

        provider = self.provider(slug)
        return self.session.scalar(
            select(ThresholdConfig.monitoring_interval_minutes).where(
                ThresholdConfig.provider_id == provider.id, ThresholdConfig.is_enabled.is_(True)
            )
        )

    def last_attempts(self, slug: str) -> dict[str, tuple[datetime | None, int, str | None]]:
        return {
            op: (state.last_attempt_at, state.consecutive_failures, state.status)
            for op, state in self.sync_states(slug).items()
        }


def backoff_multiplier(consecutive_failures: int) -> int:
    """1, 2, 4, 8 ... capped at 8x the normal interval while a provider keeps failing."""
    if consecutive_failures <= 0:
        return 1
    return min(2 ** (consecutive_failures - 1), 8)


def is_due(
    last_attempt: datetime | None, failures: int, interval: timedelta, now: datetime
) -> bool:
    if last_attempt is None:
        return True
    return now - last_attempt >= interval * backoff_multiplier(failures)
