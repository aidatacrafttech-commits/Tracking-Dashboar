from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import get_notification_service, owner_context, require_read
from app.models.enums import DeliveryStatus
from app.schemas.alerts import (
    AlertEventDetail,
    AlertPage,
    NotificationStatusRead,
    TestEmailRead,
    TestEmailRequest,
)
from app.schemas.common import PROVIDER_SLUG_PATTERN
from app.services.context import RequestContext
from app.services.notifications import NotificationService

alerts_router = APIRouter(prefix="/alerts", tags=["alerts"])
notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])

AlertId = Annotated[int, Path(ge=1)]


@alerts_router.get(
    "",
    response_model=AlertPage,
    dependencies=[Depends(require_read)],
    summary="Alert history (all providers) with delivery attempts",
)
def list_alerts(
    provider: Annotated[str | None, Query(pattern=PROVIDER_SLUG_PATTERN)] = None,
    status: DeliveryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    service: NotificationService = Depends(get_notification_service),
) -> AlertPage:
    return service.list_alerts(slug=provider, status=status, limit=limit, offset=offset)


@alerts_router.get(
    "/{alert_id}",
    response_model=AlertEventDetail,
    dependencies=[Depends(require_read)],
    summary="One alert with its rendered email and every delivery attempt",
)
def get_alert(
    alert_id: AlertId, service: NotificationService = Depends(get_notification_service)
) -> AlertEventDetail:
    return service.get_alert(alert_id)


@alerts_router.post(
    "/{alert_id}/retry",
    response_model=AlertEventDetail,
    summary="Re-queue a failed alert email (owner only)",
)
def retry_alert(
    alert_id: AlertId,
    ctx: RequestContext = Depends(owner_context),
    service: NotificationService = Depends(get_notification_service),
) -> AlertEventDetail:
    return service.retry(alert_id, ctx)


@notifications_router.get(
    "/status",
    response_model=NotificationStatusRead,
    dependencies=[Depends(require_read)],
    summary="Email configuration (no credentials) and outbox counts",
)
def notification_status(
    service: NotificationService = Depends(get_notification_service),
) -> NotificationStatusRead:
    return service.status()


@notifications_router.post(
    "/test-email",
    response_model=TestEmailRead,
    summary="Send a clearly labelled test email now (owner only)",
)
def send_test_email(
    body: TestEmailRequest,
    ctx: RequestContext = Depends(owner_context),
    service: NotificationService = Depends(get_notification_service),
) -> TestEmailRead:
    return service.send_test(body.to, ctx)
