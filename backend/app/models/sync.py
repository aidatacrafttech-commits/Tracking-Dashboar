from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.db.types import JSONText, UTCDateTime


class ProviderSyncState(Base):
    """Latest outcome per provider + operation. Drives scheduling and staleness display.

    A failed poll updates this row but leaves earlier snapshots untouched, so the UI can
    show the last good value together with its age and the current error.
    """

    __tablename__ = "provider_sync_states"
    __table_args__ = (
        sa.UniqueConstraint("provider_id", "operation"),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False
    )
    # balance | usage | billing | health | resources (app.providers.types.Operation)
    operation: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # ok | partial | error | unsupported | not_configured | disabled (SyncStatus)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(sa.String(32))
    last_error_message: Mapped[str | None] = mapped_column(sa.String(500))
    last_run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_runs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )


class InfrastructureResourceSnapshot(Base):
    """Point-in-time state of a monitored cloud resource (EC2 instance, EBS volume)."""

    __tablename__ = "infrastructure_resource_snapshots"
    __table_args__ = (
        sa.Index("ix_resource_snapshots_provider_observed", "provider_id", "observed_at"),
        sa.Index("ix_resource_snapshots_resource_observed", "resource_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    monitoring_run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_runs.id", ondelete="SET NULL")
    )
    resource_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    region: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(sa.String(255))
    state: Mapped[str | None] = mapped_column(sa.String(32))
    health_status: Mapped[str | None] = mapped_column(sa.String(32))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONText())
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
