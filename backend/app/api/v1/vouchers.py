"""Voucher management endpoints.

/api/v1/vouchers/courses/...       Course vouchers (apply to subjects)
/api/v1/vouchers/live-tutor/...    Live Tutor vouchers (apply to Beginner/Intermediate/Pro)
/api/v1/vouchers/...               summary, analytics, redemption history, users, audit,
                                   validate and redeem (shared by both categories)
    dashboard: owner writes (session + CSRF, or owner bearer), owner and viewer read.
/api/v1/integrations/vouchers/...  your app/scripts: VOUCHER_API_TOKEN bearer only; can
                                   register voucher users, validate and redeem vouchers and read
                                   capacity, nothing else.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings, owner_context, require_read
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import request_id_var
from app.core.security import client_ip
from app.models.enums import (
    Actor,
    VoucherActor,
    VoucherCategory,
    VoucherDiscountType,
    VoucherEventOutcome,
)
from app.schemas.common import UTCInput
from app.schemas.voucher import (
    AnalyticsRead,
    AuditPage,
    Capacity,
    CheckResult,
    LockRequest,
    RedeemRequest,
    RedeemResult,
    SubjectList,
    UsageEventPage,
    UserLimitUpdate,
    VoucherCheckRequest,
    VoucherCreate,
    VoucherPage,
    VoucherRead,
    VoucherStatus,
    VoucherSummary,
    VoucherUpdate,
    VoucherUserCreate,
    VoucherUserPage,
    VoucherUserRead,
)
from app.services.context import RequestContext
from app.services.settings import SettingsService
from app.services.vouchers import SortKey, VoucherContext, VoucherService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vouchers", tags=["vouchers"])
integration_router = APIRouter(prefix="/integrations/vouchers", tags=["voucher integration"])

VoucherId = Annotated[int, Path(ge=1)]
UserId = Annotated[int, Path(ge=1)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Search = Annotated[str | None, Query(max_length=100)]
CategoryFilter = Annotated[VoucherCategory | None, Query()]
CATEGORY_PATHS = {VoucherCategory.COURSE: "courses", VoucherCategory.LIVE_TUTOR: "live-tutor"}
_bearer = HTTPBearer(auto_error=False)


def get_voucher_service(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> VoucherService:
    return VoucherService(db, settings)


def display_timezone(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> str:
    return SettingsService(db, settings).display_timezone()


def owner_voucher_context(ctx: RequestContext = Depends(owner_context)) -> VoucherContext:
    return VoucherContext(VoucherActor.OWNER, ctx)


def voucher_api_context(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> VoucherContext:
    """Only the dedicated VOUCHER_API_TOKEN is accepted here (not dashboard sessions or the
    owner/viewer tokens). Failed attempts are rate-limited like other bearer failures."""
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    if settings.voucher_api_token is None:
        raise AuthenticationError(
            "The voucher API is not enabled (set VOUCHER_API_TOKEN on the server)",
            code="voucher_api_disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthenticationError("A voucher API bearer token is required")
    limiter = request.app.state.attempt_limiter
    limiter.check(ip, "voucher_api")
    expected = settings.voucher_api_token.get_secret_value()
    if not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        limiter.record(ip, "voucher_api", False)
        logger.warning("voucher API authentication failed", extra={"client_ip": ip})
        raise AuthenticationError("Invalid voucher API token")
    return VoucherContext(
        VoucherActor.VOUCHER_API,
        RequestContext(actor=Actor.SYSTEM, request_id=request_id_var.get(), client_ip=ip),
    )


# --- shared (both categories) -------------------------------------------------------------


@router.get("/summary", response_model=VoucherSummary, dependencies=[Depends(require_read)])
def summary(
    category: CategoryFilter = None,
    timezone: str = Depends(display_timezone),
    service: VoucherService = Depends(get_voucher_service),
) -> VoucherSummary:
    return service.summary(category, timezone)


@router.get("/subjects", response_model=SubjectList, dependencies=[Depends(require_read)])
def subjects(service: VoucherService = Depends(get_voucher_service)) -> SubjectList:
    return service.subjects()


@router.get("/analytics", response_model=AnalyticsRead, dependencies=[Depends(require_read)])
def analytics(
    granularity: Literal["day", "week", "month"] = "day",
    start: Annotated[UTCInput | None, Query()] = None,
    end: Annotated[UTCInput | None, Query()] = None,
    category: CategoryFilter = None,
    voucher_id: Annotated[int | None, Query(ge=1)] = None,
    timezone: str = Depends(display_timezone),
    service: VoucherService = Depends(get_voucher_service),
) -> AnalyticsRead:
    return service.analytics(
        granularity=granularity,
        start=start,
        end=end,
        category=category,
        voucher_id=voucher_id,
        timezone=timezone,
    )


@router.get(
    "/usage-events",
    response_model=UsageEventPage,
    dependencies=[Depends(require_read)],
    summary="Redemption history (successful and failed attempts)",
)
def usage_events(
    voucher_id: Annotated[int | None, Query(ge=1)] = None,
    category: CategoryFilter = None,
    outcome: VoucherEventOutcome | None = None,
    q: Search = None,
    start: Annotated[UTCInput | None, Query()] = None,
    end: Annotated[UTCInput | None, Query()] = None,
    limit: Limit = 50,
    offset: Offset = 0,
    service: VoucherService = Depends(get_voucher_service),
) -> UsageEventPage:
    return service.usage_events(
        voucher_id=voucher_id,
        category=category,
        outcome=outcome,
        q=q,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )


@router.get("/audit", response_model=AuditPage, dependencies=[Depends(require_read)])
def audit(
    q: Search = None,
    action: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]+$")] = None,
    start: Annotated[UTCInput | None, Query()] = None,
    end: Annotated[UTCInput | None, Query()] = None,
    limit: Limit = 50,
    offset: Offset = 0,
    service: VoucherService = Depends(get_voucher_service),
) -> AuditPage:
    return service.audit(q=q, action=action, start=start, end=end, limit=limit, offset=offset)


@router.get("/capacity", response_model=Capacity, dependencies=[Depends(require_read)])
def capacity(service: VoucherService = Depends(get_voucher_service)) -> Capacity:
    return service.capacity()


@router.put("/settings/user-limit", response_model=Capacity)
def set_user_limit(
    body: UserLimitUpdate,
    ctx: VoucherContext = Depends(owner_voucher_context),
    service: VoucherService = Depends(get_voucher_service),
) -> Capacity:
    return service.set_user_limit(body.user_limit, ctx)


@router.get("/users", response_model=VoucherUserPage, dependencies=[Depends(require_read)])
def list_users(
    q: Search = None,
    limit: Limit = 25,
    offset: Offset = 0,
    service: VoucherService = Depends(get_voucher_service),
) -> VoucherUserPage:
    return service.list_users(q=q, limit=limit, offset=offset)


@router.post("/users", response_model=VoucherUserRead, status_code=status.HTTP_201_CREATED)
def register_user(
    body: VoucherUserCreate,
    ctx: VoucherContext = Depends(owner_voucher_context),
    service: VoucherService = Depends(get_voucher_service),
) -> VoucherUserRead:
    return service.register_user(body, ctx)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: UserId,
    ctx: VoucherContext = Depends(owner_voucher_context),
    service: VoucherService = Depends(get_voucher_service),
) -> Response:
    service.delete_user(user_id, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/validate",
    response_model=CheckResult,
    summary="Check a voucher against an order without redeeming it (owner)",
)
def validate_from_dashboard(
    body: VoucherCheckRequest,
    _ctx: VoucherContext = Depends(owner_voucher_context),
    service: VoucherService = Depends(get_voucher_service),
) -> CheckResult:
    return service.check(body)


@router.post("/redeem", response_model=RedeemResult, summary="Record a voucher redemption (owner)")
def redeem_from_dashboard(
    body: RedeemRequest,
    ctx: VoucherContext = Depends(owner_voucher_context),
    service: VoucherService = Depends(get_voucher_service),
) -> RedeemResult:
    return service.redeem(body, ctx)


# --- per category: /courses and /live-tutor ------------------------------------------------


def _category_routes(category: VoucherCategory) -> None:
    base = f"/{CATEGORY_PATHS[category]}"
    label = "Course" if category is VoucherCategory.COURSE else "Live Tutor"
    tags = [f"{label} vouchers"]

    @router.get(
        base,
        response_model=VoucherPage,
        dependencies=[Depends(require_read)],
        tags=tags,
        name=f"list_{category.value}_vouchers",
    )
    def list_vouchers(
        q: Search = None,
        status_filter: Annotated[VoucherStatus | None, Query(alias="status")] = None,
        discount_type: VoucherDiscountType | None = None,
        applies_to: Annotated[
            str | None, Query(max_length=80, description="Subject name or package id")
        ] = None,
        valid_start: Annotated[UTCInput | None, Query()] = None,
        valid_end: Annotated[UTCInput | None, Query()] = None,
        sort: SortKey = "created",
        order: Literal["asc", "desc"] = "desc",
        limit: Limit = 25,
        offset: Offset = 0,
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherPage:
        return service.list(
            category,
            q=q,
            status=status_filter,
            discount_type=discount_type,
            applies_to=applies_to,
            valid_start=valid_start,
            valid_end=valid_end,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @router.post(
        base,
        response_model=VoucherRead,
        status_code=status.HTTP_201_CREATED,
        tags=tags,
        name=f"create_{category.value}_voucher",
    )
    def create_voucher(
        body: VoucherCreate,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.create(category, body, ctx)

    @router.get(
        base + "/{voucher_id}",
        response_model=VoucherRead,
        dependencies=[Depends(require_read)],
        tags=tags,
        name=f"get_{category.value}_voucher",
    )
    def get_voucher(
        voucher_id: VoucherId, service: VoucherService = Depends(get_voucher_service)
    ) -> VoucherRead:
        return service.get(voucher_id, category)

    @router.put(
        base + "/{voucher_id}",
        response_model=VoucherRead,
        tags=tags,
        name=f"update_{category.value}_voucher",
    )
    def update_voucher(
        voucher_id: VoucherId,
        body: VoucherUpdate,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.update(category, voucher_id, body, ctx)

    @router.delete(
        base + "/{voucher_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=tags,
        name=f"delete_{category.value}_voucher",
    )
    def delete_voucher(
        voucher_id: VoucherId,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> Response:
        service.delete(category, voucher_id, ctx)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        base + "/{voucher_id}/activate",
        response_model=VoucherRead,
        tags=tags,
        name=f"activate_{category.value}_voucher",
    )
    def activate_voucher(
        voucher_id: VoucherId,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.set_active(category, voucher_id, True, ctx)

    @router.post(
        base + "/{voucher_id}/deactivate",
        response_model=VoucherRead,
        tags=tags,
        name=f"deactivate_{category.value}_voucher",
    )
    def deactivate_voucher(
        voucher_id: VoucherId,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.set_active(category, voucher_id, False, ctx)

    @router.post(
        base + "/{voucher_id}/lock",
        response_model=VoucherRead,
        tags=tags,
        name=f"lock_{category.value}_voucher",
    )
    def lock_voucher(
        voucher_id: VoucherId,
        body: LockRequest,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.set_lock(category, voucher_id, True, body.reason, ctx)

    @router.post(
        base + "/{voucher_id}/unlock",
        response_model=VoucherRead,
        tags=tags,
        name=f"unlock_{category.value}_voucher",
    )
    def unlock_voucher(
        voucher_id: VoucherId,
        ctx: VoucherContext = Depends(owner_voucher_context),
        service: VoucherService = Depends(get_voucher_service),
    ) -> VoucherRead:
        return service.set_lock(category, voucher_id, False, None, ctx)


for _category in VoucherCategory:
    _category_routes(_category)


# --- integration API (VOUCHER_API_TOKEN) ------------------------------------------------------


@integration_router.post(
    "/users", response_model=VoucherUserRead, status_code=status.HTTP_201_CREATED
)
def integration_register_user(
    body: VoucherUserCreate,
    ctx: VoucherContext = Depends(voucher_api_context),
    service: VoucherService = Depends(get_voucher_service),
) -> VoucherUserRead:
    return service.register_user(body, ctx)


@integration_router.post("/validate", response_model=CheckResult)
def integration_validate(
    body: VoucherCheckRequest,
    _ctx: VoucherContext = Depends(voucher_api_context),
    service: VoucherService = Depends(get_voucher_service),
) -> CheckResult:
    return service.check(body)


@integration_router.post("/redeem", response_model=RedeemResult)
def integration_redeem(
    body: RedeemRequest,
    ctx: VoucherContext = Depends(voucher_api_context),
    service: VoucherService = Depends(get_voucher_service),
) -> RedeemResult:
    return service.redeem(body, ctx)


@integration_router.get("/capacity", response_model=Capacity)
def integration_capacity(
    _ctx: VoucherContext = Depends(voucher_api_context),
    service: VoucherService = Depends(get_voucher_service),
) -> Capacity:
    return service.capacity()
