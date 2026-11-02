from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import (
    MonitoringRun,
    PaymentTransaction,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ProviderUsageSnapshot,
    ServiceHealthCheck,
)
from app.models.enums import BalanceKind, EntryMethod, RunStatus, UnitType, ValueSource
from app.providers.base import CapabilityInfo, ProviderAdapter
from app.providers.deepgram import DeepgramAdapter
from app.providers.registry import ProviderRuntime, build_runtimes
from app.providers.types import (
    DataLatency,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    SourceType,
    SyncStatus,
)
from app.services.ingest import IngestService, backoff_multiplier, is_due
from app.services.polling import PollingCoordinator
from tests.conftest import provider_by_slug
from tests.fakes import (
    DEEPGRAM_BALANCES,
    DEEPGRAM_BILLING,
    DEEPGRAM_PURCHASES,
    DEEPGRAM_USAGE,
    STATUS_OK,
    FakeHttp,
    SleepRecorder,
)

DG = "https://api.deepgram.com/v1/projects/proj-1"


def deepgram_fake(balance_response: httpx.Response | None = None) -> FakeHttp:
    fake = FakeHttp()
    fake.add(
        "GET", f"{DG}/balances", balance_response or httpx.Response(200, json=DEEPGRAM_BALANCES)
    )
    fake.json(f"{DG}/usage/breakdown", DEEPGRAM_USAGE)
    fake.json(f"{DG}/billing/breakdown", DEEPGRAM_BILLING)
    fake.json(f"{DG}/purchases", DEEPGRAM_PURCHASES)
    fake.json("https://status.deepgram.com/api/v2/status.json", STATUS_OK)
    return fake


def deepgram_runtime(fake: FakeHttp) -> ProviderRuntime:
    adapter = DeepgramAdapter(
        "dg-key-0123456789abcdef0123",
        "proj-1",
        transport=fake.transport,
        sleep=SleepRecorder(),
        max_retries=0,
    )
    return ProviderRuntime(
        "deepgram", adapter, SyncStatus.OK, capabilities=dict(DeepgramAdapter.capabilities)
    )


def coordinator(
    app: FastAPI, runtimes: dict[str, ProviderRuntime], **overrides
) -> PollingCoordinator:  # type: ignore[no-untyped-def]
    settings = app.state.settings.model_copy(update=overrides)
    return PollingCoordinator(app.state.session_factory, runtimes, settings)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def count(session: Session, model) -> int:  # type: ignore[no-untyped-def]
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_full_poll_persists_everything(app: FastAPI, session: Session) -> None:
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    summary = run(coord.run_provider("deepgram"))
    assert summary.status is RunStatus.SUCCEEDED

    balance = session.scalars(select(ProviderBalanceSnapshot)).one()
    assert balance.amount == Decimal("1250.75") and balance.source is ValueSource.PROVIDER_API
    assert balance.monitoring_run_id == summary.run_id
    cost = session.scalars(
        select(ProviderUsageSnapshot).where(ProviderUsageSnapshot.metric == "cost")
    ).one()
    assert cost.cost_amount == Decimal("0.25") and cost.source is ValueSource.BILLING_REPORT
    payment = session.scalars(select(PaymentTransaction)).one()
    assert payment.entry_method is EntryMethod.PROVIDER_VERIFIED
    assert count(session, ServiceHealthCheck) == 1
    run_row = session.get(MonitoringRun, summary.run_id)
    assert run_row is not None and run_row.status is RunStatus.SUCCEEDED
    states = {s.operation: s for s in session.scalars(select(ProviderSyncState))}
    assert states["balance"].status == "ok" and states["balance"].last_success_at is not None


def test_repeat_polls_upsert_usage_and_dont_duplicate_payments(
    app: FastAPI, session: Session
) -> None:
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    run(coord.run_provider("deepgram"))
    usage_rows = count(session, ProviderUsageSnapshot)
    run(coord.run_provider("deepgram"))
    assert count(session, ProviderUsageSnapshot) == usage_rows  # same periods updated in place
    assert count(session, PaymentTransaction) == 1  # order captured once
    assert count(session, ProviderBalanceSnapshot) == 2  # balances are a time series


def test_failure_keeps_last_good_snapshot_and_marks_error(app: FastAPI, session: Session) -> None:
    fake = deepgram_fake()
    coord = coordinator(app, {"deepgram": deepgram_runtime(fake)})
    run(coord.run_provider("deepgram", [Operation.BALANCE]))

    fake.add("GET", f"{DG}/balances", httpx.Response(401, json={"err": "invalid credentials"}))
    run(coord.run_provider("deepgram", [Operation.BALANCE]))
    run(coord.run_provider("deepgram", [Operation.BALANCE]))

    session.expire_all()
    snapshots = list(session.scalars(select(ProviderBalanceSnapshot)))
    assert len(snapshots) == 1 and snapshots[0].amount == Decimal("1250.75")  # no zero written
    state = session.scalars(select(ProviderSyncState)).one()
    assert state.status == "error"
    assert state.last_error_code == "auth_failed"
    assert state.consecutive_failures == 2
    assert state.last_success_at is not None and state.last_success_at < state.last_attempt_at  # type: ignore[operator]
    last_run = session.scalars(select(MonitoringRun).order_by(MonitoringRun.id.desc())).first()
    assert last_run is not None and last_run.status is RunStatus.FAILED


def test_overlapping_polls_are_skipped(app: FastAPI, session: Session) -> None:
    release = asyncio.Event()

    class SlowAdapter(ProviderAdapter):
        slug = "deepgram"
        capabilities = {Operation.HEALTH: CapabilityInfo(True, "test")}

        async def _health(self) -> OperationResult:
            await release.wait()
            return self.result(Operation.HEALTH)

    runtime = ProviderRuntime(
        "deepgram", SlowAdapter(), SyncStatus.OK, capabilities=dict(SlowAdapter.capabilities)
    )
    coord = coordinator(app, {"deepgram": runtime})

    async def scenario():  # type: ignore[no-untyped-def]
        first = asyncio.create_task(coord.run_provider("deepgram"))
        await asyncio.sleep(0.05)
        assert coord.is_running("deepgram")
        second = await coord.run_provider("deepgram")
        release.set()
        return await first, second

    first, second = run(scenario())
    assert first.status is RunStatus.SUCCEEDED
    assert second.skipped and second.in_progress


def test_one_provider_failure_does_not_affect_another(app: FastAPI, session: Session) -> None:
    class BrokenAdapter(ProviderAdapter):
        slug = "openai"
        capabilities = {Operation.HEALTH: CapabilityInfo(True, "test")}

        async def _health(self) -> OperationResult:
            raise RuntimeError("bug in adapter")

    runtimes = {
        "openai": ProviderRuntime(
            "openai", BrokenAdapter(), SyncStatus.OK, capabilities=dict(BrokenAdapter.capabilities)
        ),
        "deepgram": deepgram_runtime(deepgram_fake()),
    }
    coord = coordinator(app, runtimes)

    async def scenario():  # type: ignore[no-untyped-def]
        return await asyncio.gather(*await coord.run_due())

    summaries = {s.provider: s for s in run(scenario())}
    assert summaries["openai"].status is RunStatus.FAILED
    assert summaries["openai"].results[0].error.message == "internal adapter error (RuntimeError)"  # type: ignore[union-attr]
    assert summaries["deepgram"].status is RunStatus.SUCCEEDED


def test_operation_timeout_budget(app: FastAPI) -> None:
    class HangingAdapter(ProviderAdapter):
        slug = "deepgram"
        capabilities = {Operation.HEALTH: CapabilityInfo(True, "test")}

        async def _health(self) -> OperationResult:
            await asyncio.sleep(10)
            return self.result(Operation.HEALTH)

    runtime = ProviderRuntime(
        "deepgram", HangingAdapter(), SyncStatus.OK, capabilities=dict(HangingAdapter.capabilities)
    )
    coord = coordinator(app, {"deepgram": runtime}, operation_timeout_seconds=0.05)
    summary = run(coord.run_provider("deepgram"))
    assert summary.results[0].error.code.value == "timeout"  # type: ignore[union-attr]


def test_not_configured_provider_records_state_without_secrets(
    app: FastAPI, session: Session
) -> None:
    runtimes = build_runtimes(app.state.settings)  # test settings carry no provider keys
    assert runtimes["openai"].status is SyncStatus.NOT_CONFIGURED
    assert runtimes["openai"].missing_config == ("OPENAI_ADMIN_KEY",)
    assert runtimes["deepgram"].missing_config == ("DEEPGRAM_API_KEY", "DEEPGRAM_PROJECT_ID")
    assert runtimes["aws"].status is SyncStatus.DISABLED
    assert runtimes["sarvam"].status is SyncStatus.OK  # status page needs no credentials

    coord = coordinator(app, runtimes)
    summary = run(coord.run_provider("openai"))
    assert summary.skipped
    openai = provider_by_slug(session, "openai")
    states = list(
        session.scalars(select(ProviderSyncState).where(ProviderSyncState.provider_id == openai.id))
    )
    assert {s.status for s in states} == {"not_configured"}
    assert all("OPENAI_ADMIN_KEY" in (s.last_error_message or "") for s in states)
    assert count(session, MonitoringRun) == 0


def test_backoff_and_due_logic() -> None:
    assert [backoff_multiplier(n) for n in range(6)] == [1, 1, 2, 4, 8, 8]
    now = datetime(2026, 10, 12, tzinfo=UTC)
    interval = timedelta(minutes=30)
    assert is_due(None, 0, interval, now)
    assert not is_due(now - timedelta(minutes=10), 0, interval, now)
    assert is_due(now - timedelta(minutes=30), 0, interval, now)
    assert not is_due(now - timedelta(minutes=90), 3, interval, now)  # 4x backoff = 120 min
    assert is_due(now - timedelta(minutes=120), 3, interval, now)


def test_due_operations_respect_intervals(app: FastAPI) -> None:
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    assert set(run(coord.due_operations("deepgram"))) == {
        Operation.HEALTH,
        Operation.BALANCE,
        Operation.USAGE,
        Operation.BILLING,
    }
    run(coord.run_provider("deepgram"))
    assert run(coord.due_operations("deepgram")) == []


def test_newly_configured_provider_is_polled_immediately(app: FastAPI) -> None:
    """Keys added + restart: the 'not configured' state from the previous process must not
    delay the first real poll by a whole interval (found during live verification)."""
    unconfigured = build_runtimes(app.state.settings)["deepgram"]
    assert unconfigured.status is SyncStatus.NOT_CONFIGURED
    run(coordinator(app, {"deepgram": unconfigured}).run_provider("deepgram"))  # records state

    configured = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    assert set(run(configured.due_operations("deepgram"))) == {
        Operation.HEALTH,
        Operation.BALANCE,
        Operation.USAGE,
        Operation.BILLING,
    }
    run(configured.run_provider("deepgram"))
    assert run(configured.due_operations("deepgram")) == []  # normal interval applies again


def test_final_usage_not_overwritten_by_provisional(app: FastAPI, session: Session) -> None:
    deepgram = provider_by_slug(session, "deepgram")
    start = datetime(2026, 10, 1, tzinfo=UTC)

    def metric(value: str, final: bool) -> Metric:
        return Metric(
            "deepgram",
            MetricType.COST,
            Decimal(value),
            "USD",
            SourceType.API_VERIFIED,
            utcnow(),
            period_start=start,
            period_end=start + timedelta(days=1),
            latency=DataLatency.DELAYED,
            is_final=final,
        )

    ingest = IngestService(session)
    for value, final in (("1.00", False), ("2.00", True), ("9.99", False)):
        ingest.record(
            "deepgram",
            None,
            OperationResult(
                "deepgram",
                Operation.BILLING,
                SyncStatus.OK,
                utcnow(),
                metrics=(metric(value, final),),
            ),
        )
    session.expire_all()
    row = session.scalars(
        select(ProviderUsageSnapshot).where(ProviderUsageSnapshot.provider_id == deepgram.id)
    ).one()
    assert row.cost_amount == Decimal("2.00") and row.is_final


def test_openai_estimate_from_manual_balance(app: FastAPI, session: Session) -> None:
    openai = provider_by_slug(session, "openai")
    base_time = utcnow() - timedelta(days=1)
    day0 = base_time.replace(hour=0, minute=0, second=0, microsecond=0)
    session.add(
        ProviderBalanceSnapshot(
            provider_id=openai.id,
            balance_kind=BalanceKind.PREPAID_BALANCE,
            amount=Decimal("100.00"),
            unit="USD",
            unit_type=UnitType.CURRENCY,
            source=ValueSource.MANUAL,
            observed_at=base_time,
        )
    )
    session.add(
        PaymentTransaction(
            provider_id=openai.id,
            kind="top_up",
            amount=Decimal("50"),
            currency="USD",
            transaction_date=base_time + timedelta(hours=1),
            entry_method=EntryMethod.MANUAL,
            created_by="owner",
        )
    )
    session.commit()

    costs = {
        "object": "page",
        "data": [
            {
                "start_time": int((day0 - timedelta(days=1)).timestamp()),
                "end_time": int(day0.timestamp()),
                "results": [{"amount": {"value": 99.0, "currency": "usd"}}],
            },  # before baseline day
            {
                "start_time": int(day0.timestamp()),
                "end_time": int((day0 + timedelta(days=1)).timestamp()),
                "results": [{"amount": {"value": 5.25, "currency": "usd"}}],
            },
            {
                "start_time": int((day0 + timedelta(days=1)).timestamp()),
                "end_time": int((day0 + timedelta(days=2)).timestamp()),
                "results": [{"amount": {"value": 7.5, "currency": "usd"}}],
            },
        ],
        "has_more": False,
        "next_page": None,
    }
    from app.providers.openai import OpenAIAdapter

    fake = FakeHttp().json("https://api.openai.com/v1/organization/costs", costs)
    adapter = OpenAIAdapter("sk-admin-x-0123456789abcdef", transport=fake.transport, max_retries=0)
    runtime = ProviderRuntime(
        "openai", adapter, SyncStatus.OK, capabilities=dict(OpenAIAdapter.capabilities)
    )
    run(coordinator(app, {"openai": runtime}).run_provider("openai", [Operation.BILLING]))

    session.expire_all()
    estimate = session.scalars(
        select(ProviderBalanceSnapshot).where(
            ProviderBalanceSnapshot.source == ValueSource.USAGE_ESTIMATE
        )
    ).one()
    # 100 - (5.25 + 7.5). The recorded 50 top-up is NOT added: payments only change a
    # balance through an explicit reconciliation.
    assert estimate.amount == Decimal("87.25")
    assert estimate.details is not None and estimate.details["conservative"] is True


@pytest.mark.parametrize("op", [Operation.BALANCE, Operation.USAGE, Operation.BILLING])
def test_unsupported_operations_recorded_as_unsupported(
    app: FastAPI, session: Session, op: Operation
) -> None:
    runtimes = build_runtimes(app.state.settings)
    coord = coordinator(app, {"sarvam": runtimes["sarvam"]})
    run(coord.run_provider("sarvam", [op]))
    state = session.scalars(select(ProviderSyncState)).one()
    assert state.status == "unsupported" and state.consecutive_failures == 0
    assert "Sarvam documents no" in (state.last_error_message or "")


def test_high_precision_provider_values_are_rounded_not_rejected(
    app: FastAPI, session: Session
) -> None:
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    run(coord.run_provider("deepgram", [Operation.USAGE]))
    hours = session.scalars(
        select(ProviderUsageSnapshot).where(ProviderUsageSnapshot.metric == "audio_hours")
    ).one()
    assert hours.quantity == Decimal("1619.724207")  # 1619.7242069444444 to 6 dp


def test_storage_failure_is_recorded_not_silent(
    app: FastAPI, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IngestService.record
    calls = {"n": 0}

    def flaky(self, slug, run_id, result):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return original(self, slug, run_id, result)

    monkeypatch.setattr(IngestService, "record", flaky)
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    summary = run(coord.run_provider("deepgram", [Operation.BALANCE]))
    assert summary.status is RunStatus.FAILED
    state = session.scalars(select(ProviderSyncState)).one()
    assert (
        state.status == "error" and state.last_error_message == "provider data could not be stored"
    )


def test_out_of_range_metric_is_skipped_individually(app: FastAPI, session: Session) -> None:
    now = utcnow()
    period = {"period_start": now - timedelta(days=1), "period_end": now}
    result = OperationResult(
        "aws",
        Operation.RESOURCES,
        SyncStatus.OK,
        now,
        metrics=(
            Metric(
                "aws",
                MetricType.S3_OBJECT_COUNT,
                Decimal("1e15"),
                "objects",
                SourceType.API_VERIFIED,
                now,
                dimension="huge",
                **period,
            ),
            Metric(
                "aws",
                MetricType.S3_OBJECT_COUNT,
                Decimal("42"),
                "objects",
                SourceType.API_VERIFIED,
                now,
                dimension="ok",
                **period,
            ),
        ),
    )
    assert IngestService(session).record("aws", None, result) == 1
    session.expire_all()
    assert [r.dimension for r in session.scalars(select(ProviderUsageSnapshot))] == ["ok"]


def test_scheduler_polls_due_providers_and_recovers_interrupted_runs(
    app: FastAPI, session: Session
) -> None:
    from app.models.enums import RunTrigger
    from app.services.polling import PollingScheduler

    deepgram = provider_by_slug(session, "deepgram")
    session.add(
        MonitoringRun(
            provider_id=deepgram.id,
            job_name="poll",
            trigger=RunTrigger.SCHEDULED,
            status=RunStatus.RUNNING,
        )
    )
    session.commit()

    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    scheduler = PollingScheduler(coord, tick_seconds=0.01)

    async def scenario() -> None:
        await scheduler.start()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not coord.is_running("deepgram") and count(session, ServiceHealthCheck):
                break
        await scheduler.stop()

    run(scenario())
    session.expire_all()
    runs = list(session.scalars(select(MonitoringRun).order_by(MonitoringRun.id)))
    assert runs[0].status is RunStatus.FAILED and runs[0].error_type == "Interrupted"
    assert runs[1].status is RunStatus.SUCCEEDED  # the scheduled poll
    assert count(session, ProviderBalanceSnapshot) == 1  # not re-polled before interval
