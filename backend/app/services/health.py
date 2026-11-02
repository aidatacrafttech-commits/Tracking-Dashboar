from __future__ import annotations

import logging

from sqlalchemy import Engine, text

from app.db.migrations import current_revision

logger = logging.getLogger(__name__)


class ReadinessService:
    def __init__(self, engine: Engine, expected_revision: str | None) -> None:
        self.engine = engine
        self.expected_revision = expected_revision

    def check(self) -> dict[str, str]:
        checks: dict[str, str] = {}
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # report, don't raise: readiness must always answer
            logger.warning(
                "readiness: database check failed", extra={"error_type": type(exc).__name__}
            )
            checks["database"] = "failed"
            checks["migrations"] = "failed"
            return checks

        try:
            current = current_revision(self.engine)
            checks["migrations"] = (
                "ok" if current == self.expected_revision else "pending_migrations"
            )
        except Exception as exc:
            logger.warning(
                "readiness: migration check failed", extra={"error_type": type(exc).__name__}
            )
            checks["migrations"] = "failed"
        return checks
