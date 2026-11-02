"""Read-only aggregates for the dashboard (overview, usage/cost series).

All arithmetic is exact Decimal on the server. Money is grouped by currency and never
summed across currencies; the browser only formats values it receives.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import AlertEvent, Provider, ProviderSyncState, ProviderUsageSnapshot
from app.models.enums import AlertState
from app.providers.registry import ProviderRuntime
from app.providers.types import Operation, SyncStatus
from app.schemas.insights import (
    MetricTotal,
    OverviewCounts,
    OverviewProvider,
    OverviewRead,
    Series,
    SeriesPoint,
    UsageSeriesRead,
)
from app.services.payments import PaymentService
from app.services.providers import ProviderService

# Metrics reported per day that can be summed over a period.
DAILY_METRICS = frozenset(
    {
        "cost",
        "credits_used",
        "audio_hours",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "requests",
        "tts_characters",
    }
)
# One "headline" usage metric per provider for the overview chart.
PRIMARY_USAGE = {"openai": "requests", "deepgram": "audio_hours", "elevenlabs": "credits_used"}
TOP_DIMENSIONS = 12


def _day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _unit(row: ProviderUsageSnapshot) -> str:
    return row.cost_currency or row.quantity_unit or ""


def _value(row: ProviderUsageSnapshot) -> Decimal:
    if row.cost_amount is not None:
        return row.cost_amount
    return row.quantity if row.quantity is not None else Decimal(0)


class InsightsService:
    def __init__(
        self, session: Session, runtimes: dict[str, ProviderRuntime] | None = None
    ) -> None:
        self.session = session
        self.runtimes = runtimes or {}
        self.providers = ProviderService(session, runtimes)

    # --- usage series ----------------------------------------------------------------------
    def usage_series(
        self, slug: str, *, days: int, metric: str | None, by_dimension: bool
    ) -> UsageSeriesRead:
        provider = self.providers.require_provider(slug)
        now = utcnow()
        since = _day(now) - timedelta(days=days - 1)
        U = ProviderUsageSnapshot
        query = select(U).where(U.provider_id == provider.id, U.period_start >= since)
        if metric:
            query = query.where(U.metric == metric)
        if not by_dimension:
            query = query.where(U.dimension == "")
        rows = list(self.session.scalars(query.order_by(U.period_start)))

        if by_dimension:
            series = self._by_dimension(rows)
        else:
            grouped: dict[tuple[str, str, str], list[SeriesPoint]] = defaultdict(list)
            for r in rows:
                grouped[(r.metric, _unit(r), r.dimension)].append(
                    SeriesPoint(
                        period_start=r.period_start,
                        period_end=r.period_end,
                        value=_value(r),
                        is_final=r.is_final,
                    )
                )
            series = [
                Series(metric=m, unit=u, dimension=d, points=pts)
                for (m, u, d), pts in sorted(grouped.items())
            ]

        month_start = _day(now).replace(day=1)
        mtd: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for r in self.session.scalars(
            select(U).where(
                U.provider_id == provider.id,
                U.dimension == "",
                U.period_start >= month_start,
                U.metric.in_(DAILY_METRICS),
            )
        ):
            mtd[(r.metric, _unit(r))] += _value(r)

        latest: list[MetricTotal] = []
        ranked = (
            select(
                U.id,
                func.row_number()
                .over(
                    partition_by=(U.metric, U.dimension),
                    order_by=(U.period_start.desc(), U.observed_at.desc()),
                )
                .label("rn"),
            )
            .where(U.provider_id == provider.id, U.metric.not_in(DAILY_METRICS))
            .subquery()
        )
        for r in self.session.scalars(
            select(U)
            .join(ranked, (ranked.c.id == U.id) & (ranked.c.rn == 1))
            .order_by(U.metric, U.dimension)
        ):
            latest.append(
                MetricTotal(
                    metric=r.metric,
                    unit=_unit(r),
                    value=_value(r),
                    dimension=r.dimension,
                    observed_at=r.observed_at,
                    is_final=r.is_final,
                    period_start=r.period_start,
                )
            )
        return UsageSeriesRead(
            provider=slug,
            days=days,
            since=since,
            series=series,
            month_to_date=[
                MetricTotal(metric=m, unit=u, value=v) for (m, u), v in sorted(mtd.items())
            ],
            latest=latest,
        )

    @staticmethod
    def _by_dimension(rows: list[ProviderUsageSnapshot]) -> list[Series]:
        rows = [r for r in rows if r.dimension != ""]
        totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
        for r in rows:
            totals[(r.metric, _unit(r), r.dimension)] += _value(r)
        keep: set[tuple[str, str, str]] = set()
        per_metric: dict[tuple[str, str], list[tuple[Decimal, str]]] = defaultdict(list)
        for (m, u, d), total in totals.items():
            per_metric[(m, u)].append((total, d))
        for (m, u), entries in per_metric.items():
            for _, d in sorted(entries, reverse=True)[:TOP_DIMENSIONS]:
                keep.add((m, u, d))
        points: dict[tuple[str, str, str], dict[datetime, SeriesPoint]] = defaultdict(dict)
        for r in rows:
            key = (r.metric, _unit(r), r.dimension)
            if key not in keep:
                key = (r.metric, _unit(r), "Other")
            existing = points[key].get(r.period_start)
            value = _value(r) + (Decimal(existing.value) if existing else Decimal(0))
            points[key][r.period_start] = SeriesPoint(
                period_start=r.period_start,
                period_end=r.period_end,
                value=value,
                is_final=r.is_final and (existing.is_final if existing else True),
            )
        return [
            Series(metric=m, unit=u, dimension=d, points=[p for _, p in sorted(pts.items())])
            for (m, u, d), pts in sorted(points.items())
        ]

    # --- overview ------------------------------------------------------------------------------
    def overview(self) -> OverviewRead:
        now = utcnow()
        providers = self.providers.list_providers()
        states: dict[str, list[ProviderSyncState]] = defaultdict(list)
        for state, slug in self.session.execute(
            select(ProviderSyncState, Provider.slug).join(
                Provider, Provider.id == ProviderSyncState.provider_id
            )
        ):
            states[slug].append(state)

        U = ProviderUsageSnapshot
        month_start = _day(now).replace(day=1)
        spend_mtd: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        usage_mtd: dict[str, dict[tuple[str, str], Decimal]] = defaultdict(
            lambda: defaultdict(Decimal)
        )
        for r, slug in self.session.execute(
            select(U, Provider.slug)
            .join(Provider, Provider.id == U.provider_id)
            .where(
                U.metric.in_({"cost", *PRIMARY_USAGE.values()}),
                U.dimension == "",
                U.period_start >= month_start,
            )
        ):
            if r.metric == "cost":
                spend_mtd[slug][_unit(r)] += _value(r)
            elif PRIMARY_USAGE.get(slug) == r.metric:
                usage_mtd[slug][(r.metric, _unit(r))] += _value(r)

        rows: list[OverviewProvider] = []
        for p in providers:
            p_states = states.get(p.id, [])
            failing = sorted(s.operation for s in p_states if s.status == SyncStatus.ERROR.value)
            successes = [s.last_success_at for s in p_states if s.last_success_at]
            errors = [s for s in p_states if s.status == SyncStatus.ERROR.value]
            bal = p.latest_balance
            rows.append(
                OverviewProvider(
                    id=p.id,
                    display_name=p.display_name,
                    balance_capability=p.balance_capability,
                    integration_status=p.integration.status if p.integration else None,
                    alert_state=p.threshold.alert_state if p.threshold else None,
                    evaluation_status=p.threshold.last_evaluation_status if p.threshold else None,
                    balance=bal.amount if bal else None,
                    balance_unit=bal.unit if bal else None,
                    balance_source_type=bal.source_type if bal else None,
                    balance_observed_at=bal.observed_at if bal else None,
                    balance_is_stale=p.latest_balance_freshness.is_stale
                    if p.latest_balance_freshness
                    else None,
                    threshold=p.threshold.amount if p.threshold else None,
                    threshold_unit=p.threshold.unit if p.threshold else None,
                    headroom=p.threshold_headroom,
                    last_successful_sync=max(successes) if successes else None,
                    failing_operations=failing,
                    last_error_code=errors[0].last_error_code if errors else None,
                    month_to_date_cost=[
                        MetricTotal(metric="cost", unit=unit, value=value)
                        for unit, value in sorted(spend_mtd.get(p.id, {}).items())
                    ],
                    month_to_date_usage=[
                        MetricTotal(metric=metric, unit=unit, value=value)
                        for (metric, unit), value in sorted(usage_mtd.get(p.id, {}).items())
                    ],
                    spend_reported=(
                        any(
                            c.supported and c.operation in (Operation.USAGE, Operation.BILLING)
                            for c in p.integration.capabilities
                        )
                        if p.integration
                        else None
                    ),
                )
            )

        counts = OverviewCounts(
            providers=len(rows),
            low_balance=sum(1 for r in rows if r.alert_state is AlertState.BREACHED),
            integration_failures=sum(1 for r in rows if r.failing_operations),
            not_configured=sum(
                1 for r in rows if r.integration_status is SyncStatus.NOT_CONFIGURED
            ),
            stale_balances=sum(1 for r in rows if r.balance_is_stale),
        )
        all_success = [r.last_successful_sync for r in rows if r.last_successful_sync]

        aws = self.session.scalar(select(Provider.id).where(Provider.slug == "aws"))

        def month_metric(metric: str) -> list[MetricTotal]:
            return [
                MetricTotal(
                    metric=metric,
                    unit=_unit(r),
                    value=_value(r),
                    observed_at=r.observed_at,
                    is_final=r.is_final,
                    period_start=r.period_start,
                )
                for r in self.session.scalars(
                    select(U)
                    .where(
                        U.provider_id == aws,
                        U.metric == metric,
                        U.dimension == "",
                        U.period_start == month_start,
                    )
                    .order_by(U.cost_currency)
                )
            ]

        since = _day(now) - timedelta(days=29)
        trend_rows = self.session.execute(
            select(U, Provider.slug)
            .join(Provider, Provider.id == U.provider_id)
            .where(
                U.dimension == "",
                U.period_start >= since,
                U.metric.in_({"cost", *PRIMARY_USAGE.values()}),
            )
            .order_by(U.period_start)
        ).all()
        spend: dict[tuple[str, str], list[SeriesPoint]] = defaultdict(list)
        usage: dict[tuple[str, str, str], list[SeriesPoint]] = defaultdict(list)
        for r, slug in trend_rows:
            point = SeriesPoint(
                period_start=r.period_start,
                period_end=r.period_end,
                value=_value(r),
                is_final=r.is_final,
            )
            if r.metric == "cost":
                spend[(slug, _unit(r))].append(point)
            elif PRIMARY_USAGE.get(slug) == r.metric:
                usage[(slug, r.metric, _unit(r))].append(point)

        alerts = self.session.execute(
            select(AlertEvent, Provider.slug)
            .join(Provider, Provider.id == AlertEvent.provider_id)
            .order_by(AlertEvent.occurred_at.desc(), AlertEvent.id.desc())
            .limit(5)
        ).all()
        from app.schemas.alerts import AlertEventRead

        recent_alerts = [
            AlertEventRead.model_validate(
                {
                    **{
                        f: getattr(e, f)
                        for f in AlertEventRead.model_fields
                        if f not in ("provider", "attempts", "source_type")
                    },
                    "provider": slug,
                    "source_type": (e.context or {}).get("source_type"),
                    "attempts": [],
                }
            )
            for e, slug in alerts
        ]
        payments = PaymentService(self.session, None).search(  # type: ignore[arg-type]
            slug=None,
            currency=None,
            start=None,
            end=None,
            q=None,
            sort="date",
            order="desc",
            limit=5,
            offset=0,
        )
        return OverviewRead(
            generated_at=now,
            counts=counts,
            last_successful_sync=max(all_success) if all_success else None,
            providers=rows,
            aws_month_to_date=month_metric("cost_month") if aws else [],
            aws_month_forecast=month_metric("cost_forecast_month") if aws else [],
            recent_payments=payments.items,
            recent_alerts=recent_alerts,
            spend_trend=[
                Series(metric="cost", unit=u, dimension="", provider=s, points=pts)
                for (s, u), pts in sorted(spend.items())
            ],
            usage_trend=[
                Series(metric=m, unit=u, dimension="", provider=s, points=pts)
                for (s, m, u), pts in sorted(usage.items())
            ],
        )
