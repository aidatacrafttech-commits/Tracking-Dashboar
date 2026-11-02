from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import DomainValidationError
from app.core.time import utcnow
from app.db.session import transaction
from app.models import (
    InfrastructureResourceSnapshot,
    Provider,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ServiceHealthCheck,
)
from app.models.enums import BalanceCapability, BalanceKind, UnitType, ValueSource
from app.providers.registry import ProviderRuntime
from app.providers.types import Operation
from app.repositories.audit import AuditLogRepository
from app.schemas.snapshot import BalanceSnapshotRead
from app.schemas.sync import (
    HealthCheckRead,
    ManualBalanceCreate,
    ProviderSyncRead,
    ResourceList,
    ResourceRead,
    SyncStateRead,
)
from app.services.alerts import AlertEvaluator
from app.services.context import RequestContext
from app.services.ingest import is_currency
from app.services.polling import PollingCoordinator
from app.services.providers import ProviderService, integration_read

FUTURE_TOLERANCE = timedelta(minutes=5)
# Sync data is "stale" when the last success is older than this many poll intervals.
STALE_INTERVALS = 3

MANUAL_BALANCE_KINDS: dict[BalanceCapability, tuple[BalanceKind, ...]] = {
    BalanceCapability.API: (BalanceKind.PREPAID_BALANCE,),
    BalanceCapability.MANUAL_ONLY: (BalanceKind.PREPAID_BALANCE,),
    # ElevenLabs: plan credits come from the API; the PAYG money balance only manually.
    BalanceCapability.PARTIAL: (BalanceKind.PREPAID_BALANCE, BalanceKind.PLAN_QUOTA),
    BalanceCapability.POSTPAID: (BalanceKind.PROMOTIONAL_CREDIT,),
}


class SyncService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        runtimes: dict[str, ProviderRuntime],
        coordinator: PollingCoordinator | None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtimes = runtimes
        self.coordinator = coordinator
        self.providers = ProviderService(session, runtimes)
        self.audit = AuditLogRepository(session)

    # --- sync status --------------------------------------------------------------------
    def sync_status(self, slug: str) -> ProviderSyncRead:
        provider = self.providers.require_provider(slug)
        runtime = self.runtimes.get(slug)
        states = {
            s.operation: s
            for s in self.session.scalars(
                select(ProviderSyncState).where(ProviderSyncState.provider_id == provider.id)
            )
        }
        now = utcnow()
        operations = []
        for op, info in runtime.capabilities.items() if runtime else []:
            state = states.get(op.value)
            interval = (
                self.coordinator.interval(slug, op)
                if self.coordinator
                else timedelta(minutes=self.settings.poll_interval_minutes)
            )
            last_success = state.last_success_at if state else None
            age = int((now - last_success).total_seconds()) if last_success else None
            operations.append(
                SyncStateRead(
                    operation=op,
                    supported=info.supported,
                    status=state.status if state else None,
                    last_attempt_at=state.last_attempt_at if state else None,
                    last_successful_sync=last_success,
                    seconds_since_success=age,
                    is_stale=info.supported
                    and (age is None or age > interval.total_seconds() * STALE_INTERVALS),
                    consecutive_failures=state.consecutive_failures if state else 0,
                    error_code=state.last_error_code if state else None,
                    error_message=state.last_error_message if state else None,
                )
            )
        return ProviderSyncRead(
            provider=slug,
            running=bool(self.coordinator and self.coordinator.is_running(slug)),
            integration=integration_read(runtime),  # type: ignore[arg-type]
            operations=operations,
        )

    # --- manual balance -----------------------------------------------------------------------
    def _validate_manual(self, provider: Provider, body: ManualBalanceCreate) -> BalanceKind:
        allowed = MANUAL_BALANCE_KINDS[provider.balance_capability]
        if body.balance_kind is not None:
            kind = body.balance_kind
        elif not is_currency(body.unit) and BalanceKind.PLAN_QUOTA in allowed:
            kind = BalanceKind.PLAN_QUOTA  # a credits value can only be the plan quota
        else:
            kind = allowed[0]
        if kind not in allowed:
            raise DomainValidationError(
                f"{provider.display_name} accepts manual balance_kind: "
                + ", ".join(k.value for k in allowed),
                code="unsupported_balance_kind",
            )
        if kind is BalanceKind.PREPAID_BALANCE and provider.default_unit_type is UnitType.CREDITS:
            # ElevenLabs PAYG balance is money even though the plan quota is credits.
            if body.unit not in self.settings.supported_currencies:
                raise DomainValidationError(
                    "Prepaid balance must be in a supported currency",
                    code="unsupported_currency",
                    details={"supported": self.settings.supported_currencies},
                )
        elif body.unit != provider.default_unit:
            raise DomainValidationError(
                f"Unit must be {provider.default_unit} for {provider.display_name}",
                code="unit_mismatch",
                details={"expected_unit": provider.default_unit},
            )
        if is_currency(body.unit) and body.amount.as_tuple().exponent < -2:  # type: ignore[operator]
            raise DomainValidationError(
                "amount must have at most 2 decimal places", code="invalid_precision"
            )
        return kind

    def record_manual_balance(
        self, slug: str, body: ManualBalanceCreate, ctx: RequestContext
    ) -> BalanceSnapshotRead:
        provider = self.providers.require_provider(slug)
        kind = self._validate_manual(provider, body)
        observed = body.observed_at or utcnow()
        if observed > utcnow() + FUTURE_TOLERANCE:
            raise DomainValidationError(
                "observed_at cannot be in the future", code="future_observed_at"
            )
        with transaction(self.session):
            snapshot = ProviderBalanceSnapshot(
                provider_id=provider.id,
                balance_kind=kind,
                amount=body.amount,
                unit=body.unit,
                unit_type=UnitType.CURRENCY if is_currency(body.unit) else UnitType.CREDITS,
                source=ValueSource.MANUAL,
                observed_at=observed,
                details={"notes": body.notes} if body.notes else None,
            )
            self.session.add(snapshot)
            self.session.flush()
            self.audit.record(
                actor=ctx.actor,
                action="balance.manual_entry",
                entity_type="provider_balance_snapshot",
                entity_id=snapshot.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={
                    "provider": slug,
                    "amount": str(body.amount),
                    "unit": body.unit,
                    "balance_kind": kind.value,
                    "observed_at": observed.isoformat(),
                },
            )
        # A manual balance is alert-eligible when "manual" is an eligible source.
        AlertEvaluator(self.session, self.settings).evaluate_provider(slug)
        return BalanceSnapshotRead.model_validate(snapshot)

    # --- health and resources -----------------------------------------------------------------
    def health_checks(self, slug: str, limit: int) -> list[HealthCheckRead]:
        provider = self.providers.require_provider(slug)
        rows = self.session.scalars(
            select(ServiceHealthCheck)
            .where(ServiceHealthCheck.provider_id == provider.id)
            .order_by(ServiceHealthCheck.checked_at.desc(), ServiceHealthCheck.id.desc())
            .limit(limit)
        )
        return [HealthCheckRead.model_validate(r) for r in rows]

    def latest_resources(self) -> ResourceList:
        aws = self.providers.require_provider("aws")
        model = InfrastructureResourceSnapshot
        latest = self.session.scalar(
            select(func.max(model.observed_at)).where(model.provider_id == aws.id)
        )
        if latest is None:
            return ResourceList(items=[], observed_at=None, is_stale=True)
        rows = self.session.scalars(
            select(model)
            .where(model.provider_id == aws.id, model.observed_at == latest)
            .order_by(model.resource_type, model.region, model.resource_id)
        )
        interval = (
            self.coordinator.interval("aws", Operation.RESOURCES)
            if self.coordinator
            else timedelta(minutes=self.settings.poll_interval_minutes)
        )
        return ResourceList(
            items=[ResourceRead.model_validate(r) for r in rows],
            observed_at=latest,
            is_stale=utcnow() - latest > interval * STALE_INTERVALS,
        )
