"""Account Deletions: the dashboard's own endpoints in front of the AdhyaapakAI admin API.

GET  /api/v1/account-deletions/status      owner and viewer: is the AdhyaapakAI link configured
GET  /api/v1/account-deletions             owner and viewer: accounts in the 7-day grace period
POST /api/v1/account-deletions/reactivate  owner (session + CSRF): restore an account
POST /api/v1/account-deletions/purge-now   owner (session + CSRF): delete permanently now

Each action is also written to this dashboard's audit log (masked email, user id, note).
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings, owner_context, require_read
from app.core.config import Settings
from app.db.session import transaction
from app.repositories.audit import AuditLogRepository
from app.schemas.common import ApiModel, InputModel
from app.services.account_deletions import AdhyaapakAdminClient, mask_email
from app.services.context import RequestContext

router = APIRouter(prefix="/account-deletions", tags=["account deletions"])
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class DeletionStatus(ApiModel):
    configured: bool
    target: str | None  # host of the AdhyaapakAI backend in use (never the secret)


class PendingDeletion(ApiModel):
    user_id: str
    email: str | None
    name: str | None
    requested_at: datetime | None
    due_at: datetime | None
    days_left: int | None


class DeletionList(ApiModel):
    count: int
    grace_days: int
    deletions: list[PendingDeletion]


class AccountAction(InputModel):
    email: str = Field(min_length=3, max_length=254)
    # The only audit trail on the AdhyaapakAI side, so it is mandatory here.
    note: str = Field(min_length=3, max_length=500)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        if not EMAIL_RE.match(value):
            raise ValueError("enter a valid email address")
        return value


class ReactivateResult(ApiModel):
    success: bool
    status: str
    user_id: str | None = None
    email: str | None = None
    already: bool = False


class PurgeResult(ApiModel):
    success: bool
    status: str  # "completed" or "pending" (already running; the sweeper finishes it)
    user_id: str | None = None
    message: str | None = None


def admin_client(
    request: Request, settings: Settings = Depends(get_settings)
) -> AdhyaapakAdminClient:
    return AdhyaapakAdminClient(settings, getattr(request.app.state, "adhyaapak_transport", None))


def _audit(
    db: Session, ctx: RequestContext, action: str, email: str, result: dict[str, object], note: str
) -> None:
    with transaction(db):
        AuditLogRepository(db).record(
            actor=ctx.actor,
            action=action,
            entity_type="student_account",
            entity_id=str(result.get("user_id") or "")[:64] or None,
            request_id=ctx.request_id,
            client_ip=ctx.client_ip,
            changes={
                "email": mask_email(email),
                "note": note,
                "status": result.get("status"),
                "already": result.get("already"),
            },
        )


@router.get("/status", response_model=DeletionStatus, dependencies=[Depends(require_read)])
def status(settings: Settings = Depends(get_settings)) -> DeletionStatus:
    configured = bool(settings.adhyaapak_api_base_url and settings.adhyaapak_admin_secret)
    target = (
        settings.adhyaapak_api_base_url.split("://", 1)[-1]
        if settings.adhyaapak_api_base_url
        else None
    )
    return DeletionStatus(configured=configured, target=target)


@router.get("", response_model=DeletionList, dependencies=[Depends(require_read)])
def list_deletions(client: AdhyaapakAdminClient = Depends(admin_client)) -> DeletionList:
    return DeletionList.model_validate(client.list_deletions())


@router.post("/reactivate", response_model=ReactivateResult)
def reactivate(
    body: AccountAction,
    ctx: RequestContext = Depends(owner_context),
    client: AdhyaapakAdminClient = Depends(admin_client),
    db: Session = Depends(get_db),
) -> ReactivateResult:
    result = client.reactivate(body.email, body.note)
    _audit(db, ctx, "account.reactivate", body.email, result, body.note)
    return ReactivateResult.model_validate(result)


@router.post("/purge-now", response_model=PurgeResult)
def purge_now(
    body: AccountAction,
    ctx: RequestContext = Depends(owner_context),
    client: AdhyaapakAdminClient = Depends(admin_client),
    db: Session = Depends(get_db),
) -> PurgeResult:
    result = client.purge_now(body.email, body.note)
    _audit(db, ctx, "account.purge_now", body.email, result, body.note)
    return PurgeResult.model_validate(result)
