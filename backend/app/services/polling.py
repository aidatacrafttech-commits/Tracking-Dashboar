"""Runs adapter operations, persists results and schedules the next polls.

* One asyncio.Lock per provider: a poll never overlaps another poll of the same provider
  (scheduled or manual). Different providers run concurrently and fail independently.
* Each operation has an overall timeout on top of the HTTP client's own timeouts.
* Repeated failures back off (2x, 4x, 8x the interval) instead of hammering a provider.
* DB work runs in worker threads so the event loop is never blocked by SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.core.logging import request_id_var
from app.core.security import SessionManager
from app.core.time import utcnow
from app.models.enums import RunStatus, RunTrigger
from app.notifications.outbox import OutboxDispatcher
from app.ops.locks import JobLocks
from app.ops.service import OperationsService
from app.providers.base import ProviderAdapter
from app.providers.registry import ProviderRuntime
from app.providers.types import (
    ErrorCode,
    Operation,
    OperationResult,
    ProviderError,
    SyncStatus,
)
from app.services.alerts import AlertEvaluator
from app.services.estimates import BalanceEstimator
from app.services.ingest import IngestService, is_due

logger = logging.getLogger(__name__)

ALL_OPERATIONS = (
    Operation.HEALTH,
    Operation.BALANCE,
    Operation.USAGE,
    Operation.BILLING,
    Operation.RESOURCES,
)
MANUAL_SYNC_COOLDOWN = timedelta(seconds=60)
# Providers whose missing balance API is covered by a usage-based estimate.
ESTIMATE_PROVIDERS = frozenset({"openai"})


UNCONFIGURED_STATES = {SyncStatus.NOT_CONFIGURED.value, SyncStatus.DISABLED.value}


@dataclass
class RunSummary:
    provider: str
    skipped: bool = False
    in_progress: bool = False  # skipped because another poll of this provider is running
    reason: str | None = None
    run_id: int | None = None
    status: RunStatus | None = None
    records_written: int = 0
    results: list[OperationResult] = field(default_factory=list)


class PollingCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        runtimes: dict[str, ProviderRuntime],
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.runtimes = runtimes
        self.settings = settings
        self.clock = clock
        self._locks = {slug: asyncio.Lock() for slug in runtimes}
        self.job_locks = JobLocks(session_factory)
        self._last_manual: dict[str, datetime] = {}

    def check_manual_rate(self, slug: str) -> None:
        """Manual syncs are limited per provider (AWS Cost Explorer bills per request)."""
        now = self.clock()
        last = self._last_manual.get(slug)
        if last is not None and now - last < MANUAL_SYNC_COOLDOWN:
            wait = int((MANUAL_SYNC_COOLDOWN - (now - last)).total_seconds()) + 1
            raise RateLimitedError(
                f"Manual sync for '{slug}' was run recently; retry in {wait}s",
                code="sync_rate_limited",
                details={"retry_after_seconds": wait},
            )
        self._last_manual[slug] = now

    # --- helpers -----------------------------------------------------------------------
    def _with_session(self, fn: Callable[[IngestService], object]) -> object:
        session = self.session_factory()
        try:
            return fn(IngestService(session))
        finally:
            session.close()

    async def _db(self, fn: Callable[[IngestService], object]) -> object:
        return await asyncio.to_thread(self._with_session, fn)

    def is_running(self, slug: str) -> bool:
        return self._locks[slug].locked()

    def interval(self, slug: str, operation: Operation) -> timedelta:
        if operation is Operation.HEALTH:
            return timedelta(minutes=self.settings.health_check_interval_minutes)
        if slug == "aws" and operation in (Operation.USAGE, Operation.BILLING):
            return timedelta(minutes=self.settings.aws_cost_explorer_interval_minutes)
        return timedelta(minutes=self.settings.poll_interval_minutes)

    def _windows(self, operation: Operation) -> tuple[datetime, datetime]:
        now = self.clock()
        if operation is Operation.BILLING:
            first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start = (first - timedelta(days=1)).replace(day=1)  # previous month start
            return start, now
        start = (now - timedelta(days=self.settings.usage_lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, now

    async def _invoke(self, adapter: ProviderAdapter, operation: Operation) -> OperationResult:
        if operation is Operation.BALANCE:
            call = adapter.fetch_balance()
        elif operation is Operation.USAGE:
            call = adapter.fetch_usage(*self._windows(operation))
        elif operation is Operation.BILLING:
            call = adapter.fetch_billing_summary(*self._windows(operation))
        elif operation is Operation.HEALTH:
            call = adapter.health_check()
        else:
            call = adapter.fetch_resources()
        try:
            return await asyncio.wait_for(call, timeout=self.settings.operation_timeout_seconds)
        except TimeoutError:
            return adapter.failure(
                operation, ProviderError(ErrorCode.TIMEOUT, "operation exceeded its time budget")
            )

    def _applicable(self, runtime: ProviderRuntime) -> list[Operation]:
        return [op for op in ALL_OPERATIONS if op in runtime.capabilities]

    # --- public API ------------------------------------------------------------------------
    async def due_operations(self, slug: str) -> list[Operation]:
        runtime = self.runtimes[slug]
        attempts = await self._db(lambda svc: svc.last_attempts(slug))
        # Per-provider monitoring interval from the threshold (editable at runtime).
        override = await self._db(lambda svc: svc.monitoring_interval(slug))
        now = self.clock()
        due = []
        for op in self._applicable(runtime):
            last, failures, status = attempts.get(op.value, (None, 0, None))  # type: ignore[union-attr]
            if runtime.adapter is not None and status in UNCONFIGURED_STATES:
                # Recorded while credentials were missing or the provider was off; it is
                # configured now (keys added + restart), so poll at once, not an interval later.
                due.append(op)
                continue
            interval = self.interval(slug, op)
            balance_driver = op is Operation.BALANCE or (
                op is Operation.BILLING and slug in ESTIMATE_PROVIDERS
            )
            if override and balance_driver:
                interval = timedelta(minutes=int(override))  # type: ignore[call-overload]
            if is_due(last, failures, interval, now):
                due.append(op)
        return due

    async def evaluate_alerts(self, slug: str) -> None:
        """Evaluate the provider's threshold right after new balance data arrives."""
        try:
            await self._db(
                lambda svc: AlertEvaluator(svc.session, self.settings).evaluate_provider(slug)
            )
        except Exception:
            logger.exception("alert evaluation failed", extra={"provider": slug})

    async def run_provider(
        self,
        slug: str,
        operations: list[Operation] | None = None,
        trigger: RunTrigger = RunTrigger.SCHEDULED,
    ) -> RunSummary:
        runtime = self.runtimes[slug]
        lock = self._locks[slug]
        if lock.locked():
            return RunSummary(
                slug,
                skipped=True,
                in_progress=True,
                reason="a poll for this provider is already running",
            )
        async with lock:
            ops = operations or self._applicable(runtime)
            if runtime.adapter is None:
                return await self._record_unavailable(runtime, ops)
            # Cross-process guard: a database lease, so two processes (or a second worker)
            # never poll the same provider at once. The asyncio lock covers this process.
            lease = f"poll:{slug}"
            ttl = timedelta(seconds=self.settings.operation_timeout_seconds * (len(ops) + 1))
            if not await asyncio.to_thread(self.job_locks.acquire, lease, ttl):
                return RunSummary(
                    slug,
                    skipped=True,
                    in_progress=True,
                    reason="a poll for this provider is running in another process",
                )
            # Correlation id for every log line of this run.
            token = request_id_var.set(f"poll-{slug}-{uuid.uuid4().hex[:8]}")
            run_id: int | None = None
            try:
                run_id = await self._db(lambda svc: svc.start_run(slug, "poll", trigger))  # type: ignore[assignment]
                return await self._run_operations(slug, runtime, ops, run_id)  # type: ignore[arg-type]
            except asyncio.CancelledError:
                # Graceful shutdown: record the run as interrupted, then let cancellation finish.
                if run_id is not None:
                    rid = run_id
                    await asyncio.shield(
                        self._db(
                            lambda svc: svc.finish_run(
                                rid, RunStatus.FAILED, 0, "cancelled during shutdown"
                            )
                        )
                    )
                raise
            finally:
                request_id_var.reset(token)
                await asyncio.shield(asyncio.to_thread(self.job_locks.release, lease))

    async def _run_operations(
        self, slug: str, runtime: ProviderRuntime, ops: list[Operation], run_id: int
    ) -> RunSummary:
        assert runtime.adapter is not None  # noqa: S101 - checked by the caller
        summary = RunSummary(slug, run_id=run_id)  # type: ignore[arg-type]
        balance_changed = False
        for op in ops:
            result = await self._invoke(runtime.adapter, op)
            summary.results.append(result)
            try:
                written = await self._db(lambda svc, r=result: svc.record(slug, run_id, r))
                summary.records_written += int(written)  # type: ignore[call-overload]
            except Exception:
                logger.exception(
                    "failed to persist result", extra={"provider": slug, "operation": op.value}
                )
                # Never fail silently: record the storage failure in the sync state.
                failed = runtime.adapter.failure(
                    op,
                    ProviderError(ErrorCode.UNEXPECTED, "provider data could not be stored"),
                )
                summary.results[-1] = failed
                try:
                    await self._db(lambda svc, r=failed: svc.record(slug, run_id, r))
                except Exception:
                    logger.exception("failed to record sync failure", extra={"provider": slug})
                continue
            if op is Operation.BALANCE and result.succeeded:
                balance_changed = True
            if op is Operation.BILLING and result.succeeded and slug in ESTIMATE_PROVIDERS:
                estimate = await self._db(
                    lambda svc: BalanceEstimator(svc.session).estimate(svc.provider(slug))
                )
                balance_changed = balance_changed or estimate is not None
        if balance_changed:
            await self.evaluate_alerts(slug)

        failed = [r for r in summary.results if r.status is SyncStatus.ERROR]
        succeeded = [r for r in summary.results if r.succeeded]
        if not failed:
            summary.status = RunStatus.SUCCEEDED
        elif succeeded:
            summary.status = RunStatus.PARTIAL
        else:
            summary.status = RunStatus.FAILED
        error = "; ".join(f"{r.operation.value}: {r.error.code.value}" for r in failed if r.error)
        await self._db(
            lambda svc: svc.finish_run(
                run_id, summary.status, summary.records_written, error or None
            )  # type: ignore[arg-type]
        )
        logger.info(
            "poll finished",
            extra={
                "provider": slug,
                "status": summary.status.value,
                "records": summary.records_written,
                "errors": error or None,
            },
        )
        return summary

    async def _record_unavailable(
        self, runtime: ProviderRuntime, ops: list[Operation]
    ) -> RunSummary:
        status = runtime.status
        message = (
            f"missing configuration: {', '.join(runtime.missing_config)}"
            if runtime.missing_config
            else (runtime.note or status.value)
        )
        code = (
            ErrorCode.NOT_CONFIGURED
            if status is SyncStatus.NOT_CONFIGURED
            else ErrorCode.UNSUPPORTED
        )
        summary = RunSummary(runtime.slug, skipped=True, reason=message)
        for op in ops:
            result = OperationResult(
                provider=runtime.slug,
                operation=op,
                status=status,
                observed_at=self.clock(),
                error=ProviderError(code, message),
            )
            summary.results.append(result)
            await self._db(lambda svc, r=result: svc.record(runtime.slug, None, r))
        return summary

    async def run_due(self) -> list[asyncio.Task[RunSummary]]:
        """Start (without awaiting) a poll for every provider that has due operations."""
        tasks = []
        for slug in self.runtimes:
            if self.is_running(slug):
                continue
            try:
                due = await self.due_operations(slug)
            except Exception:
                logger.exception("could not compute due operations", extra={"provider": slug})
                continue
            if due:
                tasks.append(asyncio.create_task(self.run_provider(slug, due), name=f"poll:{slug}"))
        return tasks

    async def aclose(self) -> None:
        for runtime in self.runtimes.values():
            if runtime.adapter is not None:
                try:
                    await runtime.adapter.aclose()
                except Exception:
                    logger.warning("adapter close failed", extra={"provider": runtime.slug})


class PollingScheduler:
    """Background loop. Every tick: start due polls, then (in its own task) re-evaluate
    thresholds (stale data, cooldown reminders) and deliver the email outbox.
    Single-process only; database constraints still prevent duplicate alerts."""

    def __init__(
        self,
        coordinator: PollingCoordinator,
        tick_seconds: float = 60.0,
        dispatcher: OutboxDispatcher | None = None,
        operations: OperationsService | None = None,
        session_manager: SessionManager | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.tick_seconds = tick_seconds
        self.dispatcher = dispatcher
        self.operations = operations
        self.session_manager = session_manager
        self._task: asyncio.Task[None] | None = None
        self._alert_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[RunSummary]] = set()

    async def start(self) -> None:
        recovered = await self.coordinator._db(lambda svc: svc.recover_interrupted_runs())
        if recovered:
            logger.warning("marked interrupted runs as failed", extra={"count": recovered})
        self._task = asyncio.create_task(self._loop(), name="polling-scheduler")

    async def _loop(self) -> None:
        while True:
            try:
                for task in await self.coordinator.run_due():
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
            except Exception:
                logger.exception("scheduler tick failed")
            if self._alert_task is None or self._alert_task.done():
                self._alert_task = asyncio.create_task(self._alert_cycle(), name="alert-cycle")
            await asyncio.sleep(self.tick_seconds)

    @property
    def state(self) -> str:
        if self._task is None:
            return "not_started"
        return "stopped" if self._task.done() else "running"

    async def _alert_cycle(self) -> None:
        coordinator = self.coordinator
        token = request_id_var.set(f"cycle-{uuid.uuid4().hex[:8]}")
        lease_taken = await asyncio.to_thread(
            coordinator.job_locks.acquire, "alert-cycle", timedelta(minutes=10)
        )
        if not lease_taken:
            request_id_var.reset(token)
            return
        try:
            await self._housekeeping()
            await self._evaluate_and_dispatch()
        finally:
            await asyncio.shield(asyncio.to_thread(coordinator.job_locks.release, "alert-cycle"))
            request_id_var.reset(token)

    async def _housekeeping(self) -> None:
        if self.session_manager is not None:
            try:
                await asyncio.to_thread(self.session_manager.purge)
            except Exception:
                logger.exception("session purge failed")
        if self.operations is not None:
            try:
                if await asyncio.to_thread(self.operations.backup_due):
                    await asyncio.to_thread(self.operations.run_backup, RunTrigger.SCHEDULED)
            except Exception:
                logger.exception("scheduled backup failed")

    async def _evaluate_and_dispatch(self) -> None:
        coordinator = self.coordinator
        try:
            await coordinator._db(
                lambda svc: AlertEvaluator(svc.session, coordinator.settings).evaluate_all()
            )
        except Exception:
            logger.exception("alert evaluation cycle failed")
        if self.dispatcher is not None:
            try:
                await asyncio.to_thread(self.dispatcher.run_once)
            except Exception:
                logger.exception("outbox dispatch failed")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._alert_task, *self._inflight) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
