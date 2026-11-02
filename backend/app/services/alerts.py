"""Threshold evaluation: persisted alert state machine + transactional outbox.

State machine per threshold (stored in threshold_configs, so restarts don't reset it):

    UNKNOWN/OK --(balance <= threshold)--------------------> BREACHED  [breach email]
    BREACHED   --(still <= threshold, cooldown elapsed)---> BREACHED  [reminder email]
    BREACHED   --(balance > threshold + recovery_margin)--> OK        [recovery email]
    UNKNOWN    --(balance > threshold)--------------------> OK        (no email)

Only a fresh, eligible, unit-compatible balance can move the state. Missing data, stale
data and incompatible currencies are recorded as an evaluation status and change nothing:
unknown is never treated as zero.

Duplicate protection: each email is an alert_events row with a UNIQUE dedupe key derived
from (threshold, breach episode, event type[, reminder number]), created in the same
transaction as the state change, guarded by the threshold's optimistic version lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import Settings
from app.core.fx import ExchangeRates
from app.core.time import utcnow
from app.db.session import transaction
from app.models import (
    AlertEvent,
    PaymentTransaction,
    Provider,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ProviderUsageSnapshot,
    ThresholdConfig,
)
from app.models.enums import (
    AlertEventType,
    AlertState,
    DeliveryStatus,
    EvaluationStatus,
    PaymentKind,
    UnitType,
    ValueSource,
)
from app.notifications.templates import AlertEmailContext, format_amount, render_email
from app.services.ingest import is_currency

logger = logging.getLogger(__name__)

SOURCE_GROUPS: dict[str, tuple[ValueSource, ...]] = {
    "api_verified": (ValueSource.PROVIDER_API, ValueSource.BILLING_REPORT),
    "manual": (ValueSource.MANUAL,),
    "usage_estimate": (ValueSource.USAGE_ESTIMATE,),
}
SOURCE_TYPE_OF = {v: k for k, values in SOURCE_GROUPS.items() for v in values}
USAGE_SUMMARY_METRICS = ("cost", "credits_used", "audio_hours", "input_tokens", "output_tokens")


@dataclass
class EvaluationResult:
    provider: str
    status: EvaluationStatus
    message: str
    alert_state: AlertState
    balance: Decimal | None = None
    unit: str | None = None
    snapshot_id: int | None = None
    events_created: list[int] = field(default_factory=list)
    skipped_concurrent: bool = False


def recipients_for(threshold: ThresholdConfig, settings: Settings) -> list[str]:
    return list(threshold.notification_emails or settings.alert_recipients)


class AlertEvaluator:
    def __init__(self, session: Session, settings: Settings, clock=utcnow) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.settings = settings
        self.clock = clock
        self.fx = ExchangeRates(settings.exchange_rates, settings.exchange_rate_source)

    # --- public -------------------------------------------------------------------------
    def evaluate_all(self) -> list[EvaluationResult]:
        slugs = list(
            self.session.scalars(
                select(Provider.slug).join(
                    ThresholdConfig, ThresholdConfig.provider_id == Provider.id
                )
            )
        )
        self.session.rollback()
        return [r for slug in slugs if (r := self.evaluate_provider(slug)) is not None]

    def evaluate_provider(self, slug: str) -> EvaluationResult | None:
        try:
            with transaction(self.session):
                provider = self.session.scalar(select(Provider).where(Provider.slug == slug))
                threshold = (
                    self.session.scalar(
                        select(ThresholdConfig).where(ThresholdConfig.provider_id == provider.id)
                    )
                    if provider
                    else None
                )
                if provider is None or threshold is None:
                    return None
                return self._evaluate(provider, threshold)
        except (StaleDataError, IntegrityError):
            # Another evaluator changed this threshold or created the same alert first.
            logger.info("alert evaluation skipped: concurrent update", extra={"provider": slug})
            return EvaluationResult(
                slug,
                EvaluationStatus.OK,
                "skipped: concurrent evaluation",
                AlertState.UNKNOWN,
                skipped_concurrent=True,
            )

    # --- internals ----------------------------------------------------------------------------
    def _latest_eligible(
        self, provider: Provider, t: ThresholdConfig
    ) -> ProviderBalanceSnapshot | None:
        sources = [s for name in (t.eligible_sources or []) for s in SOURCE_GROUPS.get(name, ())]
        if not sources:
            return None
        return self.session.scalar(
            select(ProviderBalanceSnapshot)
            .where(
                ProviderBalanceSnapshot.provider_id == provider.id,
                ProviderBalanceSnapshot.balance_kind == t.balance_kind,
                ProviderBalanceSnapshot.source.in_(sources),
                ProviderBalanceSnapshot.amount.is_not(None),
            )
            .order_by(ProviderBalanceSnapshot.observed_at.desc(), ProviderBalanceSnapshot.id.desc())
            .limit(1)
        )

    def _finish(
        self,
        t: ThresholdConfig,
        now: datetime,
        status: EvaluationStatus,
        message: str,
        snapshot: ProviderBalanceSnapshot | None,
    ) -> None:
        t.last_evaluated_at = now
        t.last_evaluation_status = status
        t.last_evaluation_message = message[:255]
        if snapshot is not None:
            t.last_balance_snapshot_id = snapshot.id
        self.session.flush()

    def _evaluate(self, provider: Provider, t: ThresholdConfig) -> EvaluationResult:
        now = self.clock()

        def done(
            status: EvaluationStatus,
            message: str,
            snapshot=None,
            value=None,  # type: ignore[no-untyped-def]
            events=None,
        ) -> EvaluationResult:
            self._finish(t, now, status, message, snapshot)
            return EvaluationResult(
                provider.slug,
                status,
                message,
                t.alert_state,
                value,
                t.unit,
                snapshot.id if snapshot else None,
                events or [],
            )

        if not t.is_enabled:
            return done(EvaluationStatus.DISABLED, "threshold disabled")

        snapshot = self._latest_eligible(provider, t)
        if snapshot is None or snapshot.amount is None:
            return done(
                EvaluationStatus.NO_DATA,
                f"no {t.balance_kind.value} balance from eligible sources "
                f"({', '.join(t.eligible_sources or [])}); balance unknown, not treated as zero",
            )

        reference = snapshot.data_as_of or snapshot.observed_at
        age = now - reference
        max_age = timedelta(minutes=provider.stale_after_minutes)
        if age > max_age:
            return done(
                EvaluationStatus.STALE,
                f"latest eligible balance is {int(age.total_seconds() // 60)} min old "
                f"(limit {provider.stale_after_minutes} min); not acting on stale data",
                snapshot,
            )

        value, conversion_note, problem = self._comparable(snapshot, t)
        if problem is not None:
            return done(problem[0], problem[1], snapshot)
        if value is None:  # pragma: no cover - _comparable returns a value or a problem
            return done(EvaluationStatus.NO_DATA, "balance unavailable", snapshot)

        boundary = t.amount + t.recovery_margin
        events: list[int] = []
        if value <= t.amount:
            if t.alert_state is not AlertState.BREACHED:
                t.alert_state = AlertState.BREACHED
                t.state_changed_at = now
                t.breach_episode += 1
                if t.notify_on_breach:
                    events.append(
                        self._create_event(
                            provider,
                            t,
                            snapshot,
                            AlertEventType.BREACHED,
                            value,
                            f"t{t.id}:e{t.breach_episode}:breached",
                            now,
                            conversion_note,
                        )
                    )
            elif (
                t.notify_on_breach
                and t.cooldown_minutes
                and (
                    t.last_notified_at is None
                    or now - t.last_notified_at >= timedelta(minutes=t.cooldown_minutes)
                )
            ):
                n = (
                    self.session.scalar(
                        select(func.count())
                        .select_from(AlertEvent)
                        .where(
                            AlertEvent.threshold_config_id == t.id,
                            AlertEvent.episode == t.breach_episode,
                            AlertEvent.event_type == AlertEventType.REMINDER,
                        )
                    )
                    or 0
                ) + 1
                events.append(
                    self._create_event(
                        provider,
                        t,
                        snapshot,
                        AlertEventType.REMINDER,
                        value,
                        f"t{t.id}:e{t.breach_episode}:reminder:{n}",
                        now,
                        conversion_note,
                    )
                )
            return done(
                EvaluationStatus.BREACHED,
                f"{format_amount(value, t.unit)} <= threshold {format_amount(t.amount, t.unit)}",
                snapshot,
                value,
                events,
            )

        if value > boundary:
            if t.alert_state is AlertState.BREACHED:
                t.alert_state = AlertState.OK
                t.state_changed_at = now
                if t.notify_on_recovery:
                    events.append(
                        self._create_event(
                            provider,
                            t,
                            snapshot,
                            AlertEventType.RECOVERED,
                            value,
                            f"t{t.id}:e{t.breach_episode}:recovered",
                            now,
                            conversion_note,
                        )
                    )
            elif t.alert_state is AlertState.UNKNOWN:
                t.alert_state = AlertState.OK
                t.state_changed_at = now
            return done(
                EvaluationStatus.OK,
                f"{format_amount(value, t.unit)} above recovery boundary",
                snapshot,
                value,
                events,
            )

        # Hysteresis band: above the threshold but not above threshold + margin.
        if t.alert_state is AlertState.UNKNOWN:
            t.alert_state = AlertState.OK
            t.state_changed_at = now
        status = (
            EvaluationStatus.BREACHED
            if t.alert_state is AlertState.BREACHED
            else EvaluationStatus.OK
        )
        return done(
            status,
            f"{format_amount(value, t.unit)} within recovery margin; state kept",
            snapshot,
            value,
        )

    def _comparable(self, snapshot: ProviderBalanceSnapshot, t: ThresholdConfig):  # type: ignore[no-untyped-def]
        """Return (value_in_threshold_unit, conversion_note, problem)."""
        amount = snapshot.amount
        if amount is None:
            return None, None, (EvaluationStatus.NO_DATA, "balance unavailable")
        if snapshot.unit == t.unit:
            return amount, None, None
        snap_money = snapshot.unit_type is UnitType.CURRENCY and is_currency(snapshot.unit)
        if not snap_money or t.unit_type is not UnitType.CURRENCY:
            return (
                None,
                None,
                (
                    EvaluationStatus.INCOMPATIBLE_UNITS,
                    f"cannot compare {snapshot.unit} with {t.unit}",
                ),
            )
        conversion = self.fx.convert(amount, snapshot.unit, t.unit)
        if conversion is None:
            return (
                None,
                None,
                (
                    EvaluationStatus.CURRENCY_MISMATCH,
                    f"balance is in {snapshot.unit}, threshold in {t.unit}; no "
                    "configured exchange rate (EXCHANGE_RATES), comparison refused",
                ),
            )
        note = (
            f"{format_amount(amount, snapshot.unit)} converted at {conversion.pair} "
            f"{conversion.rate}{' (inverted)' if conversion.inverted else ''}; "
            f"source: {conversion.source}"
        )
        return conversion.amount, note, None

    # --- event creation (outbox) -------------------------------------------------------------
    def build_context(
        self,
        provider: Provider,
        t: ThresholdConfig,
        snapshot: ProviderBalanceSnapshot | None,
        event_type: AlertEventType,
        value: Decimal | None,
        now: datetime,
        conversion_note: str | None = None,
    ) -> AlertEmailContext:
        from app.services.settings import SettingsService

        last_sync = self.session.scalar(
            select(ProviderSyncState.last_success_at).where(
                ProviderSyncState.provider_id == provider.id,
                ProviderSyncState.operation == "balance",
            )
        )
        payment = self.session.scalar(
            select(PaymentTransaction)
            .where(
                PaymentTransaction.provider_id == provider.id,
                PaymentTransaction.kind != PaymentKind.REFUND,
            )
            .order_by(PaymentTransaction.transaction_date.desc())
            .limit(1)
        )
        usage_rows = self.session.execute(
            select(
                ProviderUsageSnapshot.metric,
                func.coalesce(
                    ProviderUsageSnapshot.cost_currency, ProviderUsageSnapshot.quantity_unit
                ),
                ProviderUsageSnapshot.cost_amount,
                ProviderUsageSnapshot.quantity,
            ).where(
                ProviderUsageSnapshot.provider_id == provider.id,
                ProviderUsageSnapshot.dimension == "",
                ProviderUsageSnapshot.metric.in_(USAGE_SUMMARY_METRICS),
                ProviderUsageSnapshot.period_start >= now - timedelta(days=7),
            )
        ).all()
        totals: dict[tuple[str, str], Decimal] = {}
        for metric, unit, cost, qty in usage_rows:
            key = (metric, unit or "")
            totals[key] = totals.get(key, Decimal(0)) + (
                cost if cost is not None else qty or Decimal(0)
            )
        usage = [
            (metric.replace("_", " ").capitalize(), format_amount(total, unit))
            for (metric, unit), total in sorted(totals.items())
        ]
        reference = (snapshot.data_as_of or snapshot.observed_at) if snapshot else None
        return AlertEmailContext(
            provider_slug=provider.slug,
            provider_name=provider.display_name,
            event_type=event_type.value,
            balance=value,
            threshold=t.amount,
            unit=t.unit,
            recovery_boundary=t.amount + t.recovery_margin,
            source_type=SOURCE_TYPE_OF.get(snapshot.source) if snapshot else None,
            observed_at=snapshot.observed_at if snapshot else None,
            last_sync_at=last_sync,
            data_age_seconds=int((now - reference).total_seconds()) if reference else None,
            display_timezone=SettingsService(self.session, self.settings).display_timezone(),
            last_payment=(
                {
                    "amount": format_amount(payment.amount, payment.currency),
                    "date": payment.transaction_date.strftime("%Y-%m-%d"),
                    "method": payment.entry_method.value.replace("_", " "),
                }
                if payment
                else None
            ),
            usage_summary=usage,
            conversion_note=conversion_note,
            dashboard_url=self.settings.dashboard_url,
            breach_episode=t.breach_episode,
        )

    def _create_event(
        self,
        provider: Provider,
        t: ThresholdConfig,
        snapshot: ProviderBalanceSnapshot,
        event_type: AlertEventType,
        value: Decimal,
        dedupe_key: str,
        now: datetime,
        conversion_note: str | None,
    ) -> int:
        context = self.build_context(provider, t, snapshot, event_type, value, now, conversion_note)
        subject, text, html_body = render_email(context)
        recipients = recipients_for(t, self.settings)
        status, reason = DeliveryStatus.PENDING, None
        if not self.settings.email_delivery_enabled:
            status, reason = (
                DeliveryStatus.SUPPRESSED,
                "email delivery disabled (EMAIL_DELIVERY_ENABLED=false)",
            )
        elif not recipients:
            status, reason = (
                DeliveryStatus.SUPPRESSED,
                "no recipients (set ALERT_RECIPIENTS or notification_emails)",
            )
        event = AlertEvent(
            provider_id=provider.id,
            threshold_config_id=t.id,
            balance_snapshot_id=snapshot.id,
            event_type=event_type,
            observed_amount=value,
            threshold_amount=t.amount,
            unit=t.unit,
            recipients=recipients,
            delivery_status=status,
            last_error=reason,
            dedupe_key=dedupe_key,
            episode=t.breach_episode,
            subject=subject,
            body_text=text,
            body_html=html_body,
            context=context.to_json(),
            next_attempt_at=now if status is DeliveryStatus.PENDING else None,
            occurred_at=now,
        )
        self.session.add(event)
        self.session.flush()  # UNIQUE(dedupe_key) is enforced here
        if event_type is not AlertEventType.RECOVERED:
            t.last_notified_at = now
        logger.info(
            "alert event created",
            extra={
                "provider": provider.slug,
                "event_type": event_type.value,
                "delivery_status": status.value,
            },
        )
        return event.id
