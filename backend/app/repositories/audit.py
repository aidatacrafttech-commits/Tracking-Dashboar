from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import redact_value
from app.models import AuditLog
from app.models.enums import Actor


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor: Actor,
        action: str,
        entity_type: str,
        entity_id: str | int | None,
        request_id: str | None,
        client_ip: str | None,
        changes: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Add an audit row to the caller's transaction (committed together)."""
        entry = AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            request_id=request_id,
            client_ip=client_ip,
            changes=redact_value(changes) if changes is not None else None,
        )
        self.session.add(entry)
        return entry
