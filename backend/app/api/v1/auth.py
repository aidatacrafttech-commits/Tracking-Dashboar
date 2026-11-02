"""Sign-in, session info and sign-out for the dashboard UI."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import request_id_var
from app.core.security import (
    AttemptLimiter,
    Principal,
    Role,
    SessionManager,
    client_ip,
    cookie_name,
    csrf_token_for,
    get_principal,
    resolve_principal,
    verify_origin,
)
from app.db.session import transaction
from app.models.enums import Actor
from app.repositories.audit import AuditLogRepository
from app.schemas.common import ApiModel, InputModel

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(InputModel):
    token: str = Field(min_length=1, max_length=512)
    remember: bool = False


class SessionRead(ApiModel):
    role: Role
    can_write: bool
    via: str
    # Present only for cookie sessions: echo it as X-CSRF-Token on every write.
    csrf_token: str | None = None
    expires_at: datetime | None = None


def _audit(request: Request, actor: Actor, action: str, changes: dict | None = None) -> None:
    session = request.app.state.session_factory()
    try:
        with transaction(session):
            AuditLogRepository(session).record(
                actor=actor,
                action=action,
                entity_type="auth_session",
                entity_id=None,
                request_id=request_id_var.get(),
                client_ip=client_ip(request),
                changes=changes,
            )
    finally:
        session.close()


def _set_cookie(response: Response, settings: Settings, raw: str, max_age: int | None) -> None:
    response.set_cookie(
        cookie_name(settings),
        raw,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


@router.post(
    "/login", response_model=SessionRead, summary="Exchange a dashboard token for a session"
)
def login(body: LoginRequest, request: Request, response: Response) -> SessionRead:
    settings: Settings = request.app.state.settings
    limiter: AttemptLimiter = request.app.state.attempt_limiter
    ip = client_ip(request)
    verify_origin(request, settings)  # login CSRF: only this dashboard may sign you in
    limiter.check(ip, "login")
    principal = resolve_principal(body.token, settings)
    if principal is None:
        limiter.record(ip, "login", False)
        _audit(request, Actor.SYSTEM, "auth.login_failed")
        raise AuthenticationError("That token was not accepted")
    limiter.record(ip, "login", True)
    manager: SessionManager = request.app.state.session_manager
    raw, expires = manager.create(
        principal.role,
        body.token,
        remember=body.remember,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    max_age = settings.session_remember_days * 86400 if body.remember else None
    _set_cookie(response, settings, raw, max_age)
    _audit(request, Actor(principal.role.value), "auth.login", {"remember": body.remember})
    return SessionRead(
        role=principal.role,
        can_write=principal.can_write,
        via="session",
        csrf_token=csrf_token_for(raw),
        expires_at=expires,
    )


@router.get("/session", response_model=SessionRead, summary="Who is signed in (no secrets)")
def session_info(request: Request, principal: Principal = Depends(get_principal)) -> SessionRead:
    raw = getattr(request.state, "raw_session", None)
    return SessionRead(
        role=principal.role,
        can_write=principal.can_write,
        via=principal.via,
        csrf_token=csrf_token_for(raw) if raw else None,
    )


@router.post("/logout", status_code=204, summary="End this browser session")
def logout(request: Request, principal: Principal = Depends(get_principal)) -> Response:
    settings: Settings = request.app.state.settings
    if principal.session_hash:
        request.app.state.session_manager.revoke(principal.session_hash)
    _audit(request, Actor(principal.role.value), "auth.logout")
    response = Response(status_code=204)
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response
