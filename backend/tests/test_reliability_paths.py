"""Failure-isolation paths: one broken part (a job, a provider, the database, a housekeeping
task) must be logged and contained, never stop monitoring or crash the process."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import create_engine

from app.models.enums import EvaluationStatus
from app.ops.locks import JobLocks
from app.providers.registry import build_runtimes
from app.providers.types import SyncStatus
from app.services import health as health_module
from app.services import polling as polling_module
from app.services.health import ReadinessService
from app.services.polling import PollingScheduler
from tests.conftest import make_settings
from tests.test_polling import coordinator, deepgram_fake, deepgram_runtime, run


class Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> None:
        self.calls += 1


class EvaluatorSpy:
    calls = 0

    def __init__(self, *_: object) -> None:
        pass

    def evaluate_all(self) -> list:
        EvaluatorSpy.calls += 1
        return []


# --- scheduler ------------------------------------------------------------------------------
def test_alert_cycle_skips_while_another_process_holds_the_lease(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    EvaluatorSpy.calls = 0
    monkeypatch.setattr(polling_module, "AlertEvaluator", EvaluatorSpy)
    other = JobLocks(app.state.session_factory, owner="other-process")
    assert other.acquire("alert-cycle", timedelta(minutes=10))
    dispatcher = Recorder()
    scheduler = PollingScheduler(coordinator(app, {}), dispatcher=dispatcher)  # type: ignore[arg-type]

    run(scheduler._alert_cycle())
    assert (EvaluatorSpy.calls, dispatcher.calls) == (0, 0)

    other.release("alert-cycle")
    run(scheduler._alert_cycle())
    assert (EvaluatorSpy.calls, dispatcher.calls) == (1, 1)


def test_alert_cycle_contains_failures_of_each_step(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenEvaluator(EvaluatorSpy):
        def evaluate_all(self) -> list:
            raise RuntimeError("evaluation bug")

    class BrokenOps:
        def backup_due(self) -> bool:
            raise OSError("backup dir unavailable")

    class BrokenSessions:
        def purge(self) -> None:
            raise RuntimeError("purge failed")

    class BrokenDispatcher:
        def run_once(self) -> None:
            raise RuntimeError("smtp layer bug")

    monkeypatch.setattr(polling_module, "AlertEvaluator", BrokenEvaluator)
    coord = coordinator(app, {})
    scheduler = PollingScheduler(
        coord,
        dispatcher=BrokenDispatcher(),  # type: ignore[arg-type]
        operations=BrokenOps(),  # type: ignore[arg-type]
        session_manager=BrokenSessions(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.ERROR):
        run(scheduler._alert_cycle())  # must not raise
    messages = {r.getMessage() for r in caplog.records}
    assert {
        "session purge failed",
        "scheduled backup failed",
        "alert evaluation cycle failed",
        "outbox dispatch failed",
    } <= messages
    # The lease was released despite every step failing: the next cycle can run.
    assert coord.job_locks.acquire("alert-cycle", timedelta(minutes=1))


def test_scheduler_loop_survives_a_failing_tick_and_stops_cleanly(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    EvaluatorSpy.calls = 0
    monkeypatch.setattr(polling_module, "AlertEvaluator", EvaluatorSpy)
    coord = coordinator(app, {})
    ticks = 0

    async def broken_run_due() -> list:
        nonlocal ticks
        ticks += 1
        raise RuntimeError("tick bug")

    coord.run_due = broken_run_due  # type: ignore[method-assign]
    scheduler = PollingScheduler(coord, tick_seconds=0.01)

    async def scenario() -> None:
        await scheduler.start()
        assert scheduler.state == "running"
        for _ in range(200):
            if ticks >= 3 and EvaluatorSpy.calls >= 1:
                break
            await asyncio.sleep(0.01)
        await scheduler.stop()

    with caplog.at_level(logging.ERROR):
        run(scenario())
    assert ticks >= 3  # kept ticking after failures
    assert EvaluatorSpy.calls >= 1  # alert cycle still ran
    assert scheduler.state == "stopped"
    assert "scheduler tick failed" in {r.getMessage() for r in caplog.records}


def test_due_operation_failure_for_one_provider_does_not_block_others(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    runtimes = {
        "elevenlabs": deepgram_runtime(deepgram_fake()),  # its due check will fail
        "deepgram": deepgram_runtime(deepgram_fake()),
    }
    coord = coordinator(app, runtimes)
    original = coord.due_operations

    async def due(slug: str):  # type: ignore[no-untyped-def]
        if slug == "elevenlabs":
            raise RuntimeError("bad state row")
        return await original(slug)

    coord.due_operations = due  # type: ignore[method-assign]

    async def scenario():  # type: ignore[no-untyped-def]
        return await asyncio.gather(*await coord.run_due())

    with caplog.at_level(logging.ERROR):
        summaries = run(scenario())
    assert [s.provider for s in summaries] == ["deepgram"]
    assert "could not compute due operations" in {r.getMessage() for r in caplog.records}


def test_evaluation_error_after_poll_is_contained(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken(EvaluatorSpy):
        def evaluate_provider(self, slug: str) -> None:
            raise RuntimeError("evaluator bug")

    monkeypatch.setattr(polling_module, "AlertEvaluator", Broken)
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    with caplog.at_level(logging.ERROR):
        summary = run(coord.run_provider("deepgram"))
    assert summary.status.value == "succeeded"  # the balance was still stored
    assert "alert evaluation failed" in {r.getMessage() for r in caplog.records}


def test_adapter_close_failure_is_contained(app: FastAPI) -> None:
    runtime = deepgram_runtime(deepgram_fake())

    async def broken_close() -> None:
        raise RuntimeError("close failed")

    runtime.adapter.aclose = broken_close  # type: ignore[method-assign,union-attr]
    run(coordinator(app, {"deepgram": runtime}).aclose())  # must not raise


# --- provider registry ---------------------------------------------------------------------
def test_registry_builds_every_adapter_without_network(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    settings = make_settings(
        tmp_path / "x.db",
        openai_admin_key=SecretStr("sk-admin-dummy-not-real-000000000000"),
        deepgram_api_key=SecretStr("dg-dummy-not-real-0000000000"),
        deepgram_project_id="proj-dummy",
        elevenlabs_api_key=SecretStr("el-dummy-not-real-0000000000"),
        aws_monitoring_enabled=True,
    )
    runtimes = build_runtimes(
        settings,
        transport=httpx.MockTransport(handler),
        aws_client_factory=lambda service, region: pytest.fail("AWS called at build time"),
    )
    assert {slug: r.status for slug, r in runtimes.items()} == {
        "openai": SyncStatus.OK,
        "sarvam": SyncStatus.OK,
        "deepgram": SyncStatus.OK,
        "elevenlabs": SyncStatus.OK,
        "aws": SyncStatus.OK,
    }
    assert calls == []  # building adapters contacts nobody

    async def close_all() -> None:
        for r in runtimes.values():
            if r.adapter is not None:
                await r.adapter.aclose()

    run(close_all())


def test_registry_reports_missing_and_disabled_without_secrets(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path / "x.db",
        deepgram_api_key=SecretStr("dg-dummy-not-real-0000000000"),
        disabled_providers=["elevenlabs"],
        aws_monitoring_enabled=True,
        aws_profile="profile-that-does-not-exist-phase6",
    )
    runtimes = build_runtimes(settings)
    assert runtimes["openai"].missing_config == ("OPENAI_ADMIN_KEY",)
    assert runtimes["deepgram"].status is SyncStatus.NOT_CONFIGURED
    assert runtimes["deepgram"].missing_config == ("DEEPGRAM_PROJECT_ID",)
    assert runtimes["elevenlabs"].status is SyncStatus.DISABLED
    aws = runtimes["aws"]
    assert aws.status is SyncStatus.NOT_CONFIGURED and aws.adapter is None
    assert aws.note == "could not create AWS session (ProfileNotFound)"


# --- readiness -----------------------------------------------------------------------------
def test_readiness_reports_unreachable_database(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'missing-dir' / 'x.db').as_posix()}")
    assert ReadinessService(engine, "0005").check() == {
        "database": "failed",
        "migrations": "failed",
    }


def test_readiness_reports_migration_check_failure(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_engine: object) -> str:
        raise RuntimeError("alembic table unreadable")

    monkeypatch.setattr(health_module, "current_revision", broken)
    assert ReadinessService(app.state.engine, "0005").check() == {
        "database": "ok",
        "migrations": "failed",
    }


def test_evaluation_status_values_are_stable() -> None:
    # The UI and alert history depend on these exact values.
    assert {s.value for s in EvaluationStatus} >= {
        "ok",
        "breached",
        "stale",
        "no_data",
        "disabled",
        "currency_mismatch",
        "incompatible_units",
    }
