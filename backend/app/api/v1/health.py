"""Unauthenticated liveness/readiness probes. They expose no configuration or data."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.time import utcnow
from app.ops.service import OperationsService
from app.ops.storage import disk_status
from app.schemas.health import HealthRead, ReadinessRead

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthRead, summary="Liveness probe")
def health(request: Request) -> HealthRead:
    return HealthRead(status="ok", version=request.app.state.settings.app_version, time=utcnow())


@router.get(
    "/ready",
    response_model=ReadinessRead,
    responses={503: {"model": ReadinessRead}},
    summary="Readiness probe: database, migrations, disk space, scheduler, backups",
)
def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks = state.readiness.check()
    if checks.get("migrations") == "ok":
        try:
            checks["disk"] = disk_status(state.settings).status
            backup = OperationsService(state.session_factory, state.settings).backup_info()
            checks["backups"] = (
                "disabled" if not backup.enabled else "warning" if backup.overdue else "ok"
            )
        except Exception:  # a probe must always answer
            logger.exception("readiness: operational checks failed")
            checks["disk"] = "failed"
    scheduler = getattr(state, "scheduler", None)
    if scheduler is None:
        checks["scheduler"] = "disabled"
    else:
        checks["scheduler"] = "ok" if scheduler.state == "running" else "failed"
    # Not ready only on hard failures; warnings stay visible but keep serving.
    is_ready = all(value not in ("failed", "pending_migrations") for value in checks.values())
    body = ReadinessRead(status="ready" if is_ready else "not_ready", checks=checks)
    return JSONResponse(status_code=200 if is_ready else 503, content=body.model_dump(mode="json"))
