"""Threshold state machine: crossing, equality, repeats, cooldown, recovery, persistence,
stale data, currency/unit safety, source eligibility and duplicate protection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import AlertEvent, ProviderBalanceSnapshot, ProviderUsageSnapshot, ThresholdConfig
from app.models.enums import (
    AlertEventType,
    AlertState,
    BalanceKind,
    DeliveryStatus,
    EvaluationStatus,
    UnitType,
    ValueSource,
)
from app.services.alerts import AlertEvaluator
from tests.conftest import provider_by_slug

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def alert_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "email_delivery_enabled": True,
            "smtp_host": "127.0.0.1",
            "smtp_from": "monitor@example.com",
            "alert_recipients": ["ops@example.com"],
        }
    )


def threshold(session: Session, slug: str = "deepgram", **kw: Any) -> ThresholdConfig:
    provider = provider_by_slug(session, slug)
    values: dict[str, Any] = {
        "provider_id": provider.id,
        "balance_kind": BalanceKind.PREPAID_BALANCE,
        "amount": Decimal("25.00"),
        "unit": "USD",
        "unit_type": UnitType.CURRENCY,
        "eligible_sources": ["api_verified", "manual"],
    }
    values.update(kw)
    t = ThresholdConfig(**values)
    session.add(t)
    session.commit()
    return t


def balance(
    session: Session,
    amount: str | None,
    at: datetime,
    slug: str = "deepgram",
    source: ValueSource = ValueSource.PROVIDER_API,
    unit: str = "USD",
    kind: BalanceKind = BalanceKind.PREPAID_BALANCE,
    **kw: Any,
) -> ProviderBalanceSnapshot:
    snap = ProviderBalanceSnapshot(
        provider_id=provider_by_slug(session, slug).id,
        balance_kind=kind,
        amount=None if amount is None else Decimal(amount),
        unit=unit,
        unit_type=UnitType.CURRENCY if len(unit) == 3 else UnitType.CREDITS,
        source=source,
        observed_at=at,
        **kw,
    )
    session.add(snap)
    session.commit()
    return snap


def evaluate(app: FastAPI, settings: Settings, clock: Clock, slug: str = "deepgram"):  # type: ignore[no-untyped-def]
    """Fresh session + evaluator each time, like separate polls (or a restarted process)."""
    session: Session = app.state.session_factory()
    try:
        return AlertEvaluator(session, settings, clock=clock).evaluate_provider(slug)
    finally:
        session.close()


def events(session: Session) -> list[AlertEvent]:
    session.expire_all()
    return list(session.scalars(select(AlertEvent).order_by(AlertEvent.id)))


def test_below_threshold_creates_one_breach_alert(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    t = threshold(session)
    balance(session, "18.40", T0)
    result = evaluate(app, alert_settings, clock)
    assert result.status is EvaluationStatus.BREACHED
    assert result.alert_state is AlertState.BREACHED
    [event] = events(session)
    assert event.event_type is AlertEventType.BREACHED
    assert event.dedupe_key == f"t{t.id}:e1:breached"
    assert event.delivery_status is DeliveryStatus.PENDING
    assert event.recipients == ["ops@example.com"]
    assert event.observed_amount == Decimal("18.40")
    assert "Deepgram" in (event.subject or "") and "18.40 USD" in (event.subject or "")


def test_equal_to_threshold_breaches(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)
    balance(session, "25.00", T0)
    assert evaluate(app, alert_settings, Clock()).status is EvaluationStatus.BREACHED
    assert len(events(session)) == 1


def test_above_threshold_no_alert(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)
    balance(session, "25.01", T0)
    result = evaluate(app, alert_settings, Clock())
    assert result.status is EvaluationStatus.OK and result.alert_state is AlertState.OK
    assert events(session) == []


def test_repeated_polling_does_not_repeat_alert(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    threshold(session)  # no cooldown: one email per breach
    for i in range(5):
        balance(session, str(20 - i), clock.now)
        evaluate(app, alert_settings, clock)
        clock.advance(minutes=30)
    assert [e.event_type for e in events(session)] == [AlertEventType.BREACHED]


def test_cooldown_reminders(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    t = threshold(session, cooldown_minutes=60)
    balance(session, "10", T0)
    evaluate(app, alert_settings, clock)  # breach at T0
    for minutes, expected in ((30, 1), (31, 2), (30, 2), (31, 3)):  # +30, +61, +91, +122
        clock.advance(minutes=minutes)
        balance(session, "10", clock.now)
        evaluate(app, alert_settings, clock)
        assert len(events(session)) == expected, minutes
    keys = [e.dedupe_key for e in events(session)]
    assert keys == [f"t{t.id}:e1:breached", f"t{t.id}:e1:reminder:1", f"t{t.id}:e1:reminder:2"]


def test_recovery_with_hysteresis_and_new_episode(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    t = threshold(session, recovery_margin=Decimal("5.00"))
    balance(session, "20", clock.now)
    evaluate(app, alert_settings, clock)

    clock.advance(minutes=10)
    balance(session, "28", clock.now)  # above threshold, inside margin (<= 30)
    result = evaluate(app, alert_settings, clock)
    assert result.alert_state is AlertState.BREACHED and len(events(session)) == 1

    clock.advance(minutes=10)
    balance(session, "30.01", clock.now)  # above boundary
    result = evaluate(app, alert_settings, clock)
    assert result.alert_state is AlertState.OK
    assert events(session)[-1].event_type is AlertEventType.RECOVERED
    assert events(session)[-1].dedupe_key == f"t{t.id}:e1:recovered"

    clock.advance(minutes=10)
    balance(session, "24", clock.now)  # new crossing -> new episode, new alert
    evaluate(app, alert_settings, clock)
    assert events(session)[-1].dedupe_key == f"t{t.id}:e2:breached"
    assert [e.event_type.value for e in events(session)] == ["breached", "recovered", "breached"]


def test_state_survives_restart(app, session, alert_settings, settings) -> None:  # type: ignore[no-untyped-def]
    from app.main import create_app

    clock = Clock()
    threshold(session)
    balance(session, "5", clock.now)
    evaluate(app, alert_settings, clock)
    app.state.engine.dispose()

    restarted = create_app(settings, start_scheduler=False)  # same DB file, new process state
    try:
        clock.advance(minutes=30)
        balance(session, "4", clock.now)
        result = evaluate(restarted, alert_settings, clock)
        assert result.alert_state is AlertState.BREACHED
        assert len(events(session)) == 1  # no duplicate after restart
    finally:
        restarted.state.engine.dispose()


def test_stale_data_is_not_acted_on(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    t = threshold(session)
    balance(session, "5", T0 - timedelta(hours=3))  # deepgram stale_after = 120 min
    result = evaluate(app, alert_settings, clock)
    assert result.status is EvaluationStatus.STALE and "not acting on stale data" in result.message
    assert result.alert_state is AlertState.UNKNOWN and events(session) == []
    session.refresh(t)
    assert t.last_evaluation_status is EvaluationStatus.STALE


def test_stale_data_cannot_recover_a_breach(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    threshold(session)
    balance(session, "5", clock.now)
    evaluate(app, alert_settings, clock)
    clock.advance(hours=5)
    balance(session, "500", clock.now - timedelta(hours=4))  # high but old
    result = evaluate(app, alert_settings, clock)
    assert result.status is EvaluationStatus.STALE and result.alert_state is AlertState.BREACHED


def test_no_data_and_unavailable_are_unknown_not_zero(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session, amount=Decimal("0"))
    assert evaluate(app, alert_settings, Clock()).status is EvaluationStatus.NO_DATA
    balance(session, None, T0, unavailable_reason="provider returned no balance")
    result = evaluate(app, alert_settings, Clock())
    assert result.status is EvaluationStatus.NO_DATA and result.alert_state is AlertState.UNKNOWN
    assert events(session) == []


def test_currency_mismatch_refused_without_rates(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)  # USD
    balance(session, "100", T0, source=ValueSource.MANUAL, unit="INR")
    result = evaluate(app, alert_settings, Clock())
    assert result.status is EvaluationStatus.CURRENCY_MISMATCH
    assert "comparison refused" in result.message and events(session) == []


def test_currency_conversion_with_documented_rate(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)  # 25 USD
    balance(session, "1600", T0, source=ValueSource.MANUAL, unit="INR")
    fx = alert_settings.model_copy(
        update={
            "exchange_rates": {"USD/INR": Decimal("80")},
            "exchange_rate_source": "Owner-configured test rate",
        }
    )
    result = evaluate(app, fx, Clock())
    assert result.status is EvaluationStatus.BREACHED and result.balance == Decimal(20)
    [event] = events(session)
    assert "USD/INR 80 (inverted)" in (event.context or {}).get("conversion_note", "")


def test_credits_never_compared_with_money(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)  # USD
    balance(session, "5", T0, unit="CREDITS")
    result = evaluate(app, alert_settings, Clock())
    assert result.status is EvaluationStatus.INCOMPATIBLE_UNITS and events(session) == []


def test_aws_costs_are_never_compared_to_credit_threshold(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    aws = provider_by_slug(session, "aws")
    threshold(session, "aws", balance_kind=BalanceKind.PROMOTIONAL_CREDIT, amount=Decimal("50"))
    session.add(
        ProviderUsageSnapshot(
            provider_id=aws.id,
            metric="cost_month",
            period_start=T0 - timedelta(days=12),
            period_end=T0 + timedelta(days=19),
            cost_amount=Decimal("10"),
            cost_currency="USD",
            source=ValueSource.BILLING_REPORT,
            observed_at=T0,
        )
    )
    balance(session, "40", T0, slug="aws")  # a prepaid_balance row must also be ignored
    session.commit()
    assert evaluate(app, alert_settings, Clock(), "aws").status is EvaluationStatus.NO_DATA
    balance(
        session,
        "300",
        T0,
        slug="aws",
        kind=BalanceKind.PROMOTIONAL_CREDIT,
        source=ValueSource.BILLING_REPORT,
    )
    assert evaluate(app, alert_settings, Clock(), "aws").status is EvaluationStatus.OK


def test_eligible_sources_control_triggers(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    t = threshold(session, "openai")
    balance(session, "3", T0, slug="openai", source=ValueSource.USAGE_ESTIMATE)
    assert evaluate(app, alert_settings, Clock(), "openai").status is EvaluationStatus.NO_DATA
    session.refresh(t)  # the evaluator bumped the version in another session
    t.eligible_sources = ["manual", "usage_estimate"]
    session.commit()
    assert evaluate(app, alert_settings, Clock(), "openai").status is EvaluationStatus.BREACHED
    assert events(session)[0].context["source_type"] == "usage_estimate"  # type: ignore[index]


def test_newest_eligible_snapshot_wins(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    threshold(session)
    balance(session, "5", T0 - timedelta(minutes=30))
    balance(session, "50", T0 - timedelta(minutes=5), source=ValueSource.MANUAL)
    assert evaluate(app, alert_settings, Clock()).status is EvaluationStatus.OK


@pytest.mark.parametrize(
    ("overrides", "status", "reason"),
    [
        ({"email_delivery_enabled": False}, DeliveryStatus.SUPPRESSED, "EMAIL_DELIVERY_ENABLED"),
        ({"alert_recipients": []}, DeliveryStatus.SUPPRESSED, "no recipients"),
    ],
)
def test_alert_recorded_but_suppressed(
    app, session, alert_settings, overrides, status, reason
) -> None:  # type: ignore[no-untyped-def]
    threshold(session)
    balance(session, "1", T0)
    evaluate(app, alert_settings.model_copy(update=overrides), Clock())
    [event] = events(session)
    assert event.delivery_status is status and reason in (event.last_error or "")


def test_disabled_threshold_and_notify_off(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    t = threshold(session, is_enabled=False)
    balance(session, "1", T0)
    assert evaluate(app, alert_settings, Clock()).status is EvaluationStatus.DISABLED
    session.refresh(t)
    t.is_enabled, t.notify_on_breach = True, False
    session.commit()
    result = evaluate(app, alert_settings, Clock())
    assert result.alert_state is AlertState.BREACHED and events(session) == []


def test_duplicate_alert_creation_is_impossible(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    """Simulates a second worker that already created this alert: the UNIQUE dedupe key
    makes this evaluation roll back entirely (no second row, no state change)."""
    t = threshold(session)
    balance(session, "1", T0)
    session.add(
        AlertEvent(
            provider_id=t.provider_id,
            threshold_config_id=t.id,
            event_type=AlertEventType.BREACHED,
            threshold_amount=t.amount,
            unit="USD",
            dedupe_key=f"t{t.id}:e1:breached",
            occurred_at=T0,
        )
    )
    session.commit()
    result = evaluate(app, alert_settings, Clock())
    assert result.skipped_concurrent
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AlertEvent)) == 1
    assert session.get(ThresholdConfig, t.id).alert_state is AlertState.UNKNOWN  # rolled back


def test_email_content_has_context_but_no_secrets(app, session, alert_settings) -> None:  # type: ignore[no-untyped-def]
    from app.core.logging import register_secrets
    from app.models import PaymentTransaction
    from app.models.enums import EntryMethod

    secret = "smtp-password-should-never-appear-123"
    register_secrets(secret)
    t = threshold(session)
    session.add(
        PaymentTransaction(
            provider_id=t.provider_id,
            kind="top_up",
            amount=Decimal("100"),
            currency="USD",
            transaction_date=T0 - timedelta(days=3),
            entry_method=EntryMethod.PROVIDER_VERIFIED,
            created_by="system",
        )
    )
    session.add(
        ProviderUsageSnapshot(
            provider_id=t.provider_id,
            metric="cost",
            period_start=T0 - timedelta(days=1),
            period_end=T0,
            cost_amount=Decimal("3.5"),
            cost_currency="USD",
            source=ValueSource.BILLING_REPORT,
            observed_at=T0,
        )
    )
    balance(session, "12.5", T0, details={"note": f"pasted {secret}"})
    evaluate(
        app,
        alert_settings.model_copy(
            update={"smtp_password": secret, "dashboard_url": "https://monitor.example.com"}
        ),
        Clock(),
    )
    [event] = events(session)
    body = (event.body_text or "") + (event.body_html or "") + (event.subject or "")
    for expected in (
        "Deepgram",
        "12.50 USD",
        "25.00 USD",
        "Verified by the provider API",
        "100.00 USD",
        "Cost: 3.50 USD",
        "console.deepgram.com",
        "https://monitor.example.com",
        "Last successful sync",
    ):
        assert expected in body, expected
    assert secret not in body
