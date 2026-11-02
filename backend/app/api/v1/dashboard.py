"""Dashboard-facing endpoints: session role, overview, global payments, live updates."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_payment_service, owner_context, require_read
from app.schemas.common import PROVIDER_SLUG_PATTERN, UTCInput
from app.schemas.insights import OverviewRead
from app.schemas.payment import (
    PaymentCreate,
    PaymentPage,
    PaymentRead,
    ReconcilePreview,
    ReconcileRequest,
)
from app.schemas.snapshot import BalanceSnapshotRead
from app.services.changes import change_markers, changed_topics
from app.services.context import RequestContext
from app.services.insights import InsightsService
from app.services.payments import PaymentService

router = APIRouter()

PaymentId = Annotated[int, Path(ge=1)]


@router.get(
    "/overview",
    response_model=OverviewRead,
    tags=["dashboard"],
    dependencies=[Depends(require_read)],
    summary="Dashboard overview aggregates",
)
def overview(request: Request, db: Session = Depends(get_db)) -> OverviewRead:
    return InsightsService(db, request.app.state.runtimes).overview()


# --- payments across providers ------------------------------------------------------------


@router.get(
    "/payments",
    response_model=PaymentPage,
    tags=["payments"],
    dependencies=[Depends(require_read)],
    summary="Search payments across providers",
)
def search_payments(
    provider: Annotated[str | None, Query(pattern=PROVIDER_SLUG_PATTERN)] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
    start: Annotated[UTCInput | None, Query()] = None,
    end: Annotated[UTCInput | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    sort: Literal["date", "amount", "provider"] = "date",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    service: PaymentService = Depends(get_payment_service),
) -> PaymentPage:
    return service.search(
        slug=provider,
        currency=currency,
        start=start,
        end=end,
        q=q,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_read)],
)
def get_payment(
    payment_id: PaymentId, service: PaymentService = Depends(get_payment_service)
) -> PaymentRead:
    return service.get(payment_id)


@router.put(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    summary="Edit a manual payment (owner only)",
)
def update_payment(
    payment_id: PaymentId,
    body: PaymentCreate,
    ctx: RequestContext = Depends(owner_context),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentRead:
    return service.update_manual_payment(payment_id, body, ctx)


@router.delete(
    "/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payments"],
    summary="Delete a manual payment (owner only, audited)",
)
def delete_payment(
    payment_id: PaymentId,
    ctx: RequestContext = Depends(owner_context),
    service: PaymentService = Depends(get_payment_service),
) -> Response:
    service.delete_manual_payment(payment_id, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/payments/{payment_id}/reconcile-preview",
    response_model=ReconcilePreview,
    tags=["payments"],
    dependencies=[Depends(require_read)],
    summary="What applying this payment to the balance would do (changes nothing)",
)
def reconcile_preview(
    payment_id: PaymentId, service: PaymentService = Depends(get_payment_service)
) -> ReconcilePreview:
    return service.reconcile_preview(payment_id)


@router.post(
    "/payments/{payment_id}/reconcile",
    response_model=BalanceSnapshotRead,
    tags=["payments"],
    summary="Apply a payment to the balance (owner, explicit)",
)
def reconcile_payment(
    payment_id: PaymentId,
    body: ReconcileRequest,
    ctx: RequestContext = Depends(owner_context),
    service: PaymentService = Depends(get_payment_service),
) -> BalanceSnapshotRead:
    return service.reconcile(payment_id, body.base_snapshot_id, ctx)


# --- live updates ---------------------------------------------------------------------------


@router.get(
    "/events",
    tags=["dashboard"],
    dependencies=[Depends(require_read)],
    summary="Server-Sent Events: names of data topics that changed",
)
async def events(request: Request) -> StreamingResponse:
    settings = request.app.state.settings
    factory = request.app.state.session_factory

    async def stream() -> AsyncIterator[str]:
        started = time.monotonic()
        markers = await asyncio.to_thread(change_markers, factory)
        yield "retry: 5000\n"
        yield f"event: ready\ndata: {json.dumps({'topics': list(markers)})}\n\n"
        last_beat = time.monotonic()
        shutting_down = request.app.state.shutting_down
        while time.monotonic() - started < settings.sse_max_connection_seconds:
            try:
                await asyncio.wait_for(shutting_down.wait(), timeout=settings.sse_check_seconds)
                return  # server is stopping: end the stream so shutdown is not delayed
            except TimeoutError:
                pass
            if await request.is_disconnected():
                return
            current = await asyncio.to_thread(change_markers, factory)
            topics = changed_topics(markers, current)
            markers = current
            if topics:
                yield f"event: changed\ndata: {json.dumps({'topics': topics})}\n\n"
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat >= 15:
                yield ": keep-alive\n\n"
                last_beat = time.monotonic()
        # Bounded connection lifetime: the client reconnects transparently.
        yield "event: reconnect\ndata: {}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
