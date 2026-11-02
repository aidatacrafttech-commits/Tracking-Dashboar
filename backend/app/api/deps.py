"""FastAPI dependency providers: settings, DB session, request context, services."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import request_id_var
from app.core.security import Principal, Role, require_owner, require_read
from app.models.enums import Actor
from app.services.context import RequestContext
from app.services.notifications import NotificationService
from app.services.payments import PaymentService
from app.services.polling import PollingCoordinator
from app.services.providers import ProviderService
from app.services.settings import SettingsService
from app.services.sync import SyncService
from app.services.thresholds import ThresholdService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()  # rolls back anything not explicitly committed


def _context(request: Request, principal: Principal) -> RequestContext:
    return RequestContext(
        actor=Actor.OWNER if principal.role is Role.OWNER else Actor.VIEWER,
        request_id=request_id_var.get(),
        client_ip=request.client.host if request.client else None,
    )


def owner_context(
    request: Request, principal: Principal = Depends(require_owner)
) -> RequestContext:
    return _context(request, principal)


def get_provider_service(request: Request, db: Session = Depends(get_db)) -> ProviderService:
    return ProviderService(db, request.app.state.runtimes)


def get_notification_service(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> NotificationService:
    return NotificationService(
        db, settings, request.app.state.dispatcher, request.app.state.smtp_sender
    )


def get_coordinator(request: Request) -> PollingCoordinator:
    return request.app.state.coordinator


def get_sync_service(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> SyncService:
    return SyncService(db, settings, request.app.state.runtimes, request.app.state.coordinator)


def get_payment_service(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> PaymentService:
    return PaymentService(db, settings)


def get_threshold_service(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> ThresholdService:
    return ThresholdService(db, settings)


def get_settings_service(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> SettingsService:
    return SettingsService(db, settings)


__all__ = [
    "get_coordinator",
    "get_db",
    "get_notification_service",
    "get_payment_service",
    "get_provider_service",
    "get_settings",
    "get_settings_service",
    "get_sync_service",
    "get_threshold_service",
    "owner_context",
    "require_owner",
    "require_read",
]
