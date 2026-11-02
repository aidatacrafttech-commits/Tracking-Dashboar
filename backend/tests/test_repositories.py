from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.time import utcnow
from app.models import (
    PaymentTransaction,
    ProviderBalanceSnapshot,
    ProviderUsageSnapshot,
    ThresholdConfig,
)
from app.models.enums import (
    Actor,
    BalanceKind,
    EntryMethod,
    PaymentKind,
    UnitType,
    ValueSource,
)
from app.repositories.payments import PaymentRepository
from app.repositories.snapshots import SnapshotRepository
from tests.conftest import provider_by_slug

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _payment(provider_id: int, amount: str, **kw: object) -> PaymentTransaction:
    values: dict[str, object] = {
        "provider_id": provider_id,
        "kind": PaymentKind.TOP_UP,
        "amount": Decimal(amount),
        "currency": "USD",
        "transaction_date": T0,
        "entry_method": EntryMethod.MANUAL,
        "created_by": Actor.OWNER,
    }
    values.update(kw)
    return PaymentTransaction(**values)


def _balance(provider_id: int, amount: str | None, observed_at: datetime, **kw: object):
    values: dict[str, object] = {
        "provider_id": provider_id,
        "balance_kind": BalanceKind.PREPAID_BALANCE,
        "amount": None if amount is None else Decimal(amount),
        "unit": "USD",
        "unit_type": UnitType.CURRENCY,
        "source": ValueSource.PROVIDER_API,
        "observed_at": observed_at,
    }
    values.update(kw)
    return ProviderBalanceSnapshot(**values)


def test_money_is_stored_exactly_as_integer_micro_units(session: Session) -> None:
    deepgram = provider_by_slug(session, "deepgram")
    session.add(_payment(deepgram.id, "0.1"))
    session.add(_payment(deepgram.id, "0.2"))
    session.commit()
    raw = session.execute(text("SELECT amount FROM payment_transactions ORDER BY id")).scalars()
    assert list(raw) == [100_000, 200_000]
    totals = PaymentRepository(session).totals_by_currency(deepgram.id, start=None, end=None)
    assert totals == [("USD", Decimal("0.3"), 2)]


def test_totals_subtract_refunds_per_currency(session: Session) -> None:
    openai = provider_by_slug(session, "openai")
    session.add_all(
        [
            _payment(openai.id, "50.00"),
            _payment(openai.id, "20.00", kind=PaymentKind.REFUND),
            _payment(openai.id, "1000", currency="INR"),
        ]
    )
    session.commit()
    totals = PaymentRepository(session).totals_by_currency(openai.id, start=None, end=None)
    assert totals == [("INR", Decimal("1000"), 1), ("USD", Decimal("30"), 2)]


def test_payment_reference_unique_per_provider(session: Session) -> None:
    openai = provider_by_slug(session, "openai")
    sarvam = provider_by_slug(session, "sarvam")
    session.add(_payment(openai.id, "10", payment_reference="INV-1"))
    session.add(_payment(sarvam.id, "10", payment_reference="INV-1"))  # other provider: fine
    session.add(_payment(openai.id, "10"))  # NULL references may repeat
    session.add(_payment(openai.id, "10"))
    session.commit()
    session.add(_payment(openai.id, "10", payment_reference="INV-1"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_payment_amount_must_be_positive_at_db_level(session: Session, amount: str) -> None:
    openai = provider_by_slug(session, "openai")
    session.add(_payment(openai.id, amount))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_keys_are_enforced(session: Session) -> None:
    session.add(_payment(99999, "10"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_balance_is_null_not_zero_and_requires_reason(session: Session) -> None:
    openai = provider_by_slug(session, "openai")
    session.add(_balance(openai.id, None, T0))  # no reason given
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    snap = _balance(openai.id, None, T0, unavailable_reason="no balance API")
    session.add(snap)
    session.commit()
    session.expire_all()
    stored = session.get(ProviderBalanceSnapshot, snap.id)
    assert stored is not None and stored.amount is None


def test_latest_balance_per_provider(session: Session) -> None:
    deepgram = provider_by_slug(session, "deepgram")
    openai = provider_by_slug(session, "openai")
    repo = SnapshotRepository(session)
    repo.add_balance(_balance(deepgram.id, "10", T0))
    repo.add_balance(_balance(deepgram.id, "8.5", T0 + timedelta(hours=1)))
    repo.add_balance(_balance(openai.id, "99", T0, source=ValueSource.MANUAL))
    session.commit()
    latest = repo.latest_balances([deepgram.id, openai.id])
    assert latest[deepgram.id].amount == Decimal("8.5")
    assert latest[openai.id].amount == Decimal("99")


def test_usage_snapshot_constraints(session: Session) -> None:
    aws = provider_by_slug(session, "aws")
    base = {
        "provider_id": aws.id,
        "metric": "cost",
        "source": ValueSource.BILLING_REPORT,
        "observed_at": T0,
    }
    # period_end must be after period_start
    session.add(
        ProviderUsageSnapshot(
            **base, period_start=T0, period_end=T0, cost_amount=Decimal("1"), cost_currency="USD"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    # cost requires currency
    session.add(
        ProviderUsageSnapshot(
            **base, period_start=T0, period_end=T0 + timedelta(days=1), cost_amount=Decimal("1")
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    ok = ProviderUsageSnapshot(
        **base,
        period_start=T0,
        period_end=T0 + timedelta(days=1),
        cost_amount=Decimal("12.345678"),
        cost_currency="USD",
    )
    session.add(ok)
    session.commit()
    assert ok.dimension == ""  # provider total


def test_threshold_optimistic_locking(session: Session, app) -> None:  # type: ignore[no-untyped-def]
    deepgram = provider_by_slug(session, "deepgram")
    threshold = ThresholdConfig(
        provider_id=deepgram.id,
        balance_kind=BalanceKind.PREPAID_BALANCE,
        amount=Decimal("10"),
        unit="USD",
        unit_type=UnitType.CURRENCY,
    )
    session.add(threshold)
    session.commit()
    assert threshold.version == 1

    other: Session = app.state.session_factory()
    try:
        concurrent = other.get(ThresholdConfig, threshold.id)
        assert concurrent is not None
        concurrent.amount = Decimal("20")
        other.commit()
    finally:
        other.close()

    threshold.amount = Decimal("30")
    threshold.updated_at = utcnow()
    with pytest.raises(StaleDataError):
        session.commit()
