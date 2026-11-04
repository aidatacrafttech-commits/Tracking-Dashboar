"""App Users: read-only counts from the AdhyaapakAI MongoDB.

GET /api/v1/app-users/status   owner and viewer: is the app database configured
GET /api/v1/app-users/summary  owner and viewer: today + the last N days (7-90) or start..end
GET /api/v1/app-users/signups  owner and viewer: signups on ?date= or from ?start= to ?end=.
                               The owner sees full name, email and phone; viewers see them masked.
GET /api/v1/app-users/live     owner and viewer: online now (app requests, videos, Live Tutor,
                               doubts, tests and app heartbeats), each person once
GET /api/v1/app-users/deletions  owner and viewer: deletion requests in the same kind of range
                               (read from the app's deletion journal; nothing is changed).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_settings, require_read
from app.api.v1.vouchers import display_timezone
from app.core.config import Settings
from app.core.security import Principal
from app.core.time import utcnow
from app.schemas.app_users import (
    AppUsersStatus,
    AppUsersSummary,
    DeletionList,
    LiveNow,
    SignupList,
)
from app.services.app_users import AppUsersService, TtlCache, local_day, resolve_range

router = APIRouter(prefix="/app-users", tags=["app users"])
Days = Annotated[int | None, Query(ge=7, le=90)]


def app_users_service(request: Request) -> AppUsersService:
    state = request.app.state
    if getattr(state, "app_users_cache", None) is None:
        state.app_users_cache = TtlCache()
    return AppUsersService(getattr(state, "app_users_source", None), state.app_users_cache)


@router.get("/status", response_model=AppUsersStatus, dependencies=[Depends(require_read)])
def status(request: Request, settings: Settings = Depends(get_settings)) -> AppUsersStatus:
    configured = getattr(request.app.state, "app_users_source", None) is not None
    return AppUsersStatus(
        configured=configured, database=settings.adhyaapak_mongo_db if configured else None
    )


@router.get("/summary", response_model=AppUsersSummary, dependencies=[Depends(require_read)])
def summary(
    days: Days = None,
    start: date | None = None,
    end: date | None = None,
    tz: str = Depends(display_timezone),
    service: AppUsersService = Depends(app_users_service),
) -> AppUsersSummary:
    now = utcnow()
    first, last = resolve_range(ZoneInfo(tz), now, days, start, end)
    return AppUsersSummary.model_validate(service.summary(first, last, tz, now))


@router.get("/live", response_model=LiveNow, dependencies=[Depends(require_read)])
def live(
    request: Request,
    settings: Settings = Depends(get_settings),
    service: AppUsersService = Depends(app_users_service),
) -> LiveNow:
    app_open = request.app.state.presence.online_ids() if settings.adhyaapak_jwt_secret else None
    return LiveNow.model_validate(service.live(utcnow(), app_open=app_open))


@router.get("/signups", response_model=SignupList)
def signups(
    day: Annotated[date | None, Query(alias="date")] = None,
    start: date | None = None,
    end: date | None = None,
    principal: Principal = Depends(require_read),
    tz: str = Depends(display_timezone),
    service: AppUsersService = Depends(app_users_service),
) -> SignupList:
    now = utcnow()
    first, last = _list_range(tz, now, day, start, end)
    # Full contact details for the owner only; viewers (read-only token) get masked values.
    return SignupList.model_validate(
        service.signups_between(first, last, tz, now, full=principal.can_write)
    )


@router.get("/deletions", response_model=DeletionList)
def deletions(
    day: Annotated[date | None, Query(alias="date")] = None,
    start: date | None = None,
    end: date | None = None,
    principal: Principal = Depends(require_read),
    tz: str = Depends(display_timezone),
    service: AppUsersService = Depends(app_users_service),
) -> DeletionList:
    now = utcnow()
    first, last = _list_range(tz, now, day, start, end)
    return DeletionList.model_validate(
        service.deletions_between(first, last, tz, now, full=principal.can_write)
    )


def _list_range(
    tz: str, now: datetime, day: date | None, start: date | None, end: date | None
) -> tuple[date, date]:
    """?date= (one day), ?start=&end=, or today when neither is given."""
    zone = ZoneInfo(tz)
    if day is not None:
        start = end = day
    elif start is None and end is None:
        start = end = local_day(now, zone)
    return resolve_range(zone, now, None, start, end)
