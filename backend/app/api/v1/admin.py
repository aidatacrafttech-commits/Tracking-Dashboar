"""Owner-only operational endpoints (no secrets are ever returned)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import update

from app.api.deps import owner_context
from app.core.errors import ConflictError
from app.core.logging import request_id_var
from app.core.security import client_ip, require_owner
from app.core.time import utcnow
from app.db.session import transaction
from app.models import AuthSession
from app.models.enums import RunTrigger
from app.ops.service import OpsStatusRead
from app.ops.storage import BackupError
from app.repositories.audit import AuditLogRepository
from app.schemas.common import ApiModel
from app.services.context import RequestContext

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_owner)])


class BackupRunRead(ApiModel):
    status: str
    error: str | None


class RevokeRead(ApiModel):
    revoked: int


def _audit(request: Request, ctx: RequestContext, action: str, changes: dict | None = None) -> None:
    session = request.app.state.session_factory()
    try:
        with transaction(session):
            AuditLogRepository(session).record(
                actor=ctx.actor,
                action=action,
                entity_type="operations",
                entity_id=None,
                request_id=request_id_var.get(),
                client_ip=client_ip(request),
                changes=changes,
            )
    finally:
        session.close()


@router.get(
    "/operations", response_model=OpsStatusRead, summary="Disk, backups, permissions, scheduler"
)
def operations_status(request: Request) -> OpsStatusRead:
    scheduler = request.app.state.scheduler
    state = scheduler.state if scheduler is not None else "disabled"
    return request.app.state.operations.status(state)


@router.post("/backup", response_model=BackupRunRead, summary="Back up the database now")
def backup_now(request: Request, ctx: RequestContext = Depends(owner_context)) -> BackupRunRead:
    try:
        run = request.app.state.operations.run_backup(RunTrigger.MANUAL)
    except BackupError as exc:
        raise ConflictError(str(exc), code="backup_in_progress") from exc
    _audit(request, ctx, "ops.backup", {"status": run.status.value})
    return BackupRunRead(status=run.status.value, error=run.error_message)


@router.post(
    "/sessions/revoke-all", response_model=RevokeRead, summary="Sign out every browser session"
)
def revoke_all_sessions(
    request: Request, ctx: RequestContext = Depends(owner_context)
) -> RevokeRead:
    session = request.app.state.session_factory()
    try:
        with transaction(session):
            revoked = (
                session.execute(
                    update(AuthSession)
                    .where(AuthSession.revoked_at.is_(None))
                    .values(revoked_at=utcnow())
                ).rowcount
                or 0
            )
    finally:
        session.close()
    _audit(request, ctx, "auth.revoke_all", {"revoked": revoked})
    return RevokeRead(revoked=revoked)
