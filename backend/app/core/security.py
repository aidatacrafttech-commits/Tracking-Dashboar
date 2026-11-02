"""Single-owner authentication and authorization.

Two ways to authenticate, both resolving to a role (owner = read/write, viewer = read):

1. Browser sessions (the dashboard UI). POST /auth/login exchanges a dashboard token for a
   random session id in an HttpOnly, SameSite=Strict cookie. The server stores only a
   SHA-256 hash of it, with idle and absolute expiry. Every state-changing request on a
   session must carry the session's CSRF token (X-CSRF-Token), come from an allowed
   Origin, and not be marked cross-site by the browser (Sec-Fetch-Site).
2. Bearer tokens (scripts/automation): ``Authorization: Bearer <token>``. Not sent
   automatically by browsers, so not CSRF-able. Can be disabled (BEARER_AUTH_ENABLED).

Failed logins and failed bearer attempts are rate-limited per client IP and globally.
Rotating a token in .env invalidates every session created with the old token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AuthenticationError, PermissionDeniedError, RateLimitedError
from app.core.time import utcnow

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Dashboard owner or viewer token")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
TOUCH_INTERVAL = timedelta(seconds=60)


class Role(StrEnum):
    OWNER = "owner"
    VIEWER = "viewer"


@dataclass(frozen=True, slots=True)
class Principal:
    role: Role
    via: str = "bearer"  # bearer | session
    session_hash: str | None = None

    @property
    def can_write(self) -> bool:
        return self.role is Role.OWNER


# --- credentials -------------------------------------------------------------------------


def _matches(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def resolve_principal(token: str, settings: Settings) -> Principal | None:
    # Evaluate both comparisons so timing does not reveal which token matched.
    is_owner = _matches(token, settings.dashboard_owner_token.get_secret_value())
    is_viewer = settings.dashboard_viewer_token is not None and _matches(
        token, settings.dashboard_viewer_token.get_secret_value()
    )
    if is_owner:
        return Principal(Role.OWNER)
    if is_viewer:
        return Principal(Role.VIEWER)
    return None


def fingerprint(token: str) -> str:
    return hashlib.sha256(b"acm-credential:" + token.encode("utf-8")).hexdigest()[:32]


def current_fingerprint(role: Role, settings: Settings) -> str | None:
    if role is Role.OWNER:
        return fingerprint(settings.dashboard_owner_token.get_secret_value())
    if settings.dashboard_viewer_token is None:
        return None
    return fingerprint(settings.dashboard_viewer_token.get_secret_value())


def hash_session_id(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def csrf_token_for(raw_session_id: str) -> str:
    """Derived from the (HttpOnly) session id: unknowable to another site."""
    return hmac.new(raw_session_id.encode("utf-8"), b"acm-csrf-v1", hashlib.sha256).hexdigest()


def cookie_name(settings: Settings) -> str:
    # The __Host- prefix makes browsers enforce Secure, Path=/ and no Domain attribute.
    return "__Host-acm_session" if settings.cookie_secure else "acm_session"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- rate limiting -------------------------------------------------------------------------


class AttemptLimiter:
    """Counts failed attempts in the database so limits survive restarts."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def _window_start(self) -> datetime:
        return utcnow() - timedelta(minutes=self.settings.login_window_minutes)

    def check(self, ip: str, kind: str) -> None:
        from app.models import AuthAttempt

        per_ip = (
            self.settings.login_max_failures
            if kind == "login"
            else self.settings.login_max_failures * 4
        )
        session = self.session_factory()
        try:
            since = self._window_start()
            base = (
                select(func.count())
                .select_from(AuthAttempt)
                .where(
                    AuthAttempt.kind == kind,
                    AuthAttempt.success.is_(False),
                    AuthAttempt.occurred_at >= since,
                )
            )
            ip_failures = session.scalar(base.where(AuthAttempt.client_ip == ip)) or 0
            all_failures = session.scalar(base) or 0
        finally:
            session.close()
        if ip_failures >= per_ip or all_failures >= per_ip * 10:
            retry = self.settings.login_window_minutes * 60
            logger.warning("authentication rate limit hit", extra={"client_ip": ip, "kind": kind})
            raise RateLimitedError(
                "Too many failed attempts. Try again later.",
                code="too_many_attempts",
                details={"retry_after_seconds": retry},
            )

    def record(self, ip: str, kind: str, success: bool) -> None:
        from app.models import AuthAttempt

        session = self.session_factory()
        try:
            session.add(AuthAttempt(client_ip=ip[:64], kind=kind, success=success))
            # Keep the table small: attempts older than a day are irrelevant to limits.
            session.execute(
                delete(AuthAttempt).where(AuthAttempt.occurred_at < utcnow() - timedelta(days=1))
            )
            session.commit()
        finally:
            session.close()


# --- sessions -------------------------------------------------------------------------------


class SessionManager:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def create(
        self, role: Role, token: str, *, remember: bool, ip: str, user_agent: str | None
    ) -> tuple[str, datetime]:
        from app.models import AuthSession

        raw = secrets.token_urlsafe(32)
        now = utcnow()
        lifetime = (
            timedelta(days=self.settings.session_remember_days)
            if remember
            else timedelta(hours=self.settings.session_max_hours)
        )
        session = self.session_factory()
        try:
            session.add(
                AuthSession(
                    id_hash=hash_session_id(raw),
                    role=role.value,
                    credential_fingerprint=fingerprint(token),
                    created_at=now,
                    last_seen_at=now,
                    expires_at=now + lifetime,
                    persistent=remember,
                    client_ip=ip[:64],
                    user_agent=(user_agent or "")[:200] or None,
                )
            )
            session.commit()
        finally:
            session.close()
        return raw, now + lifetime

    def resolve(self, raw: str) -> Principal | None:
        from app.models import AuthSession

        id_hash = hash_session_id(raw)
        now = utcnow()
        session = self.session_factory()
        try:
            row = session.get(AuthSession, id_hash)
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                return None
            if now - row.last_seen_at > timedelta(minutes=self.settings.session_idle_minutes):
                return None
            role = Role(row.role)
            expected = current_fingerprint(role, self.settings)
            if expected is None or not hmac.compare_digest(expected, row.credential_fingerprint):
                return None  # the token used to sign in was rotated or removed
            if now - row.last_seen_at >= TOUCH_INTERVAL:
                session.execute(
                    update(AuthSession)
                    .where(AuthSession.id_hash == id_hash)
                    .values(last_seen_at=now)
                )
                session.commit()
            return Principal(role, via="session", session_hash=id_hash)
        finally:
            session.close()

    def revoke(self, id_hash: str) -> None:
        from app.models import AuthSession

        session = self.session_factory()
        try:
            session.execute(
                update(AuthSession)
                .where(AuthSession.id_hash == id_hash, AuthSession.revoked_at.is_(None))
                .values(revoked_at=utcnow())
            )
            session.commit()
        finally:
            session.close()

    def purge(self) -> int:
        """Delete sessions that can never be used again."""
        from app.models import AuthSession

        cutoff = utcnow() - timedelta(minutes=self.settings.session_idle_minutes)
        session = self.session_factory()
        try:
            result = session.execute(
                delete(AuthSession).where(
                    or_(
                        AuthSession.expires_at <= utcnow(),
                        AuthSession.revoked_at.is_not(None),
                        AuthSession.last_seen_at < cutoff,
                    )
                )
            )
            session.commit()
            return result.rowcount or 0
        finally:
            session.close()


# --- request checks --------------------------------------------------------------------------


def _origin_of(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def verify_origin(request: Request, settings: Settings) -> None:
    """Writes made with a cookie must come from this dashboard (or an allowed origin)."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise PermissionDeniedError("Cross-site request refused", code="cross_site_request")
    origin = _origin_of(request)
    if origin is None:
        raise PermissionDeniedError("Missing Origin header", code="origin_required")
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc.lower() == host.lower() or origin in settings.cors_origins:
        return
    raise PermissionDeniedError("Request origin is not allowed", code="origin_not_allowed")


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    settings: Settings = request.app.state.settings
    ip = client_ip(request)

    if credentials is not None or request.headers.get("authorization"):
        if not settings.bearer_auth_enabled:
            raise AuthenticationError("Bearer authentication is disabled; sign in instead")
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not credentials.credentials
        ):
            raise AuthenticationError("Malformed Authorization header")
        limiter: AttemptLimiter = request.app.state.attempt_limiter
        limiter.check(ip, "bearer")
        principal = resolve_principal(credentials.credentials, settings)
        if principal is None:
            limiter.record(ip, "bearer", False)
            logger.warning("bearer authentication failed", extra={"client_ip": ip})
            raise AuthenticationError("Invalid bearer token")
        request.state.principal = principal
        return principal

    raw = request.cookies.get(cookie_name(settings))
    if not raw or len(raw) > 128:
        raise AuthenticationError("Not signed in")
    manager: SessionManager = request.app.state.session_manager
    principal = manager.resolve(raw)
    if principal is None:
        raise AuthenticationError("Session expired or signed out", code="session_expired")
    if request.method not in SAFE_METHODS:
        verify_origin(request, settings)
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, csrf_token_for(raw)):
            raise PermissionDeniedError("Missing or invalid CSRF token", code="csrf_failed")
    request.state.principal = principal
    request.state.raw_session = raw
    return principal


def require_read(principal: Principal = Depends(get_principal)) -> Principal:
    return principal


def require_owner(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.can_write:
        raise PermissionDeniedError("This action requires the owner role")
    return principal
