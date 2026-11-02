from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_coordinator,
    get_db,
    get_notification_service,
    get_payment_service,
    get_provider_service,
    get_sync_service,
    get_threshold_service,
    owner_context,
    require_read,
)
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.models.enums import AlertEventType, DeliveryStatus, RunTrigger
from app.schemas.alerts import AlertPage, EmailPreviewRead, EvaluationRead
from app.schemas.common import PROVIDER_SLUG_PATTERN, UTCInput
from app.schemas.insights import UsageSeriesRead
from app.schemas.payment import PaymentCreate, PaymentPage, PaymentRead
from app.schemas.provider import ProviderRead
from app.schemas.snapshot import BalanceSnapshotPage, BalanceSnapshotRead, UsageSnapshotPage
from app.schemas.sync import (
    HealthCheckRead,
    ManualBalanceCreate,
    OperationOutcome,
    ProviderSyncRead,
    SyncRunRead,
)
from app.schemas.threshold import ThresholdRead, ThresholdUpsert
from app.services.audit import record_audit
from app.services.context import RequestContext
from app.services.insights import InsightsService
from app.services.notifications import NotificationService
from app.services.payments import PaymentService
from app.services.polling import PollingCoordinator
from app.services.providers import ProviderService
from app.services.sync import SyncService
from app.services.thresholds import ThresholdService

router = APIRouter(prefix="/providers", tags=["providers"], dependencies=[Depends(require_read)])

ProviderId = Annotated[
    str,
    Path(pattern=PROVIDER_SLUG_PATTERN, description="Provider slug, e.g. 'openai'"),
]
RangeStart = Annotated[UTCInput | None, Query(description="Inclusive, ISO-8601 with offset")]
RangeEnd = Annotated[UTCInput | None, Query(description="Exclusive, ISO-8601 with offset")]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]


@router.get("", response_model=list[ProviderRead], summary="List monitored providers")
def list_providers(service: ProviderService = Depends(get_provider_service)) -> list[ProviderRead]:
    return service.list_providers()


@router.get("/{provider_id}", response_model=ProviderRead, summary="Get one provider")
def get_provider(
    provider_id: ProviderId, service: ProviderService = Depends(get_provider_service)
) -> ProviderRead:
    return service.get_provider(provider_id)


@router.get(
    "/{provider_id}/snapshots",
    response_model=BalanceSnapshotPage | UsageSnapshotPage,
    summary="Balance or usage history",
)
def list_snapshots(
    provider_id: ProviderId,
    kind: Literal["balance", "usage"] = "balance",
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: Limit = 50,
    offset: Offset = 0,
    service: ProviderService = Depends(get_provider_service),
) -> BalanceSnapshotPage | UsageSnapshotPage:
    return service.list_snapshots(
        provider_id, kind=kind, start=start, end=end, limit=limit, offset=offset
    )


@router.get("/{provider_id}/payments", response_model=PaymentPage, summary="Payment history")
def list_payments(
    provider_id: ProviderId,
    start: RangeStart = None,
    end: RangeEnd = None,
    limit: Limit = 50,
    offset: Offset = 0,
    service: PaymentService = Depends(get_payment_service),
) -> PaymentPage:
    return service.list_payments(provider_id, start=start, end=end, limit=limit, offset=offset)


@router.post(
    "/{provider_id}/payments",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a manual payment (owner only)",
)
def create_payment(
    provider_id: ProviderId,
    body: PaymentCreate,
    ctx: RequestContext = Depends(owner_context),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentRead:
    return service.create_manual_payment(provider_id, body, ctx)


@router.get("/{provider_id}/threshold", response_model=ThresholdRead, summary="Get alert threshold")
def get_threshold(
    provider_id: ProviderId, service: ThresholdService = Depends(get_threshold_service)
) -> ThresholdRead:
    return service.get_threshold(provider_id)


@router.put(
    "/{provider_id}/threshold",
    response_model=ThresholdRead,
    summary="Create or replace alert threshold (owner only)",
)
def put_threshold(
    provider_id: ProviderId,
    body: ThresholdUpsert,
    ctx: RequestContext = Depends(owner_context),
    service: ThresholdService = Depends(get_threshold_service),
) -> ThresholdRead:
    return service.upsert_threshold(provider_id, body, ctx)


@router.get(
    "/{provider_id}/sync-status",
    response_model=ProviderSyncRead,
    summary="Integration config, capabilities and last sync per operation",
)
def get_sync_status(
    provider_id: ProviderId, service: SyncService = Depends(get_sync_service)
) -> ProviderSyncRead:
    return service.sync_status(provider_id)


@router.post(
    "/{provider_id}/sync",
    response_model=SyncRunRead,
    summary="Poll this provider now (owner only)",
    responses={409: {"description": "A poll is already running"}, 429: {"description": "Too soon"}},
)
async def trigger_sync(
    provider_id: ProviderId,
    ctx: RequestContext = Depends(owner_context),
    coordinator: PollingCoordinator = Depends(get_coordinator),
) -> SyncRunRead:
    if provider_id not in coordinator.runtimes:
        raise NotFoundError(f"Provider '{provider_id}' not found", code="provider_not_found")
    coordinator.check_manual_rate(provider_id)
    summary = await coordinator.run_provider(provider_id, trigger=RunTrigger.MANUAL)
    await asyncio.to_thread(
        record_audit,
        coordinator.session_factory,
        ctx,
        "sync.trigger",
        "provider",
        provider_id,
        {"status": summary.status.value if summary.status else None, "skipped": summary.skipped},
    )
    if summary.in_progress:
        raise ConflictError(summary.reason or "sync in progress", code="sync_in_progress")
    return SyncRunRead(
        provider=provider_id,
        skipped=summary.skipped,
        reason=summary.reason,
        run_id=summary.run_id,
        status=summary.status,
        records_written=summary.records_written,
        operations=[
            OperationOutcome(
                operation=r.operation,
                status=r.status,
                error_code=r.error.code.value if r.error else None,
                error_message=r.error.message if r.error else None,
                warnings=list(r.warnings),
            )
            for r in summary.results
        ],
    )


@router.post(
    "/{provider_id}/balance-entries",
    response_model=BalanceSnapshotRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a manually observed balance (owner only)",
)
def create_manual_balance(
    provider_id: ProviderId,
    body: ManualBalanceCreate,
    ctx: RequestContext = Depends(owner_context),
    service: SyncService = Depends(get_sync_service),
) -> BalanceSnapshotRead:
    return service.record_manual_balance(provider_id, body, ctx)


@router.get(
    "/{provider_id}/health-checks",
    response_model=list[HealthCheckRead],
    summary="Recent service health checks",
)
def list_health_checks(
    provider_id: ProviderId,
    limit: Limit = 50,
    service: SyncService = Depends(get_sync_service),
) -> list[HealthCheckRead]:
    return service.health_checks(provider_id, limit)


@router.post(
    "/{provider_id}/threshold/evaluate",
    response_model=EvaluationRead,
    summary="Evaluate the threshold against current data now (owner only)",
)
def evaluate_threshold(
    provider_id: ProviderId,
    ctx: RequestContext = Depends(owner_context),
    service: NotificationService = Depends(get_notification_service),
) -> EvaluationRead:
    result = service.evaluate(provider_id)
    service.audit_action(
        ctx,
        "alert.evaluate",
        "threshold",
        provider_id,
        {"status": result.status.value, "events": result.events_created},
    )
    return result


@router.get(
    "/{provider_id}/alerts", response_model=AlertPage, summary="Alert history for this provider"
)
def list_provider_alerts(
    provider_id: ProviderId,
    status: DeliveryStatus | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
    service: NotificationService = Depends(get_notification_service),
) -> AlertPage:
    return service.list_alerts(slug=provider_id, status=status, limit=limit, offset=offset)


@router.get(
    "/{provider_id}/alert-preview",
    response_model=EmailPreviewRead,
    summary="Render the alert email for current data without sending or recording it",
)
def preview_alert(
    provider_id: ProviderId,
    event_type: AlertEventType = AlertEventType.BREACHED,
    service: NotificationService = Depends(get_notification_service),
) -> EmailPreviewRead:
    return service.preview(provider_id, event_type)


@router.get(
    "/{provider_id}/usage-series",
    response_model=UsageSeriesRead,
    summary="Daily usage/cost series, month-to-date totals and latest point-in-time metrics",
)
def usage_series(
    provider_id: ProviderId,
    request: Request,
    days: Annotated[int, Query(ge=1, le=400)] = 30,
    metric: Annotated[str | None, Query(pattern=r"^[a-z0-9_]{1,64}$")] = None,
    by_dimension: bool = False,
    db: Session = Depends(get_db),
) -> UsageSeriesRead:
    if by_dimension and not metric:
        raise DomainValidationError("by_dimension requires a metric", code="metric_required")
    return InsightsService(db, request.app.state.runtimes).usage_series(
        provider_id, days=days, metric=metric, by_dimension=by_dimension
    )
