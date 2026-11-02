"""Standalone audit helper for actions that are not part of a service transaction."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import transaction
from app.repositories.audit import AuditLogRepository
from app.services.context import RequestContext


def record_audit(
    session_factory: sessionmaker[Session],
    ctx: RequestContext,
    action: str,
    entity_type: str,
    entity_id: str | int | None,
    changes: dict[str, Any] | None = None,
) -> None:
    session = session_factory()
    try:
        with transaction(session):
            AuditLogRepository(session).record(
                actor=ctx.actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes=changes,
            )
    finally:
        session.close()
