"""Monitoring runs, service health, audit trail and non-secret application settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.db.types import JSONText, UTCDateTime
from app.models.enums import (
    Actor,
    HealthSource,
    HealthStatus,
    RunStatus,
    RunTrigger,
    str_enum,
)


class MonitoringRun(Base):
    __tablename__ = "monitoring_runs"
    __table_args__ = (
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"
        ),
        sa.CheckConstraint("records_written >= 0", name="records_non_negative"),
        sa.Index("ix_monitoring_runs_provider_started", "provider_id", "started_at"),
        sa.Index("ix_monitoring_runs_job_started", "job_name", "started_at"),
        sa.Index("ix_monitoring_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL for runs not tied to one provider (e.g. alert evaluation across all providers).
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT")
    )
    job_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    trigger: Mapped[RunTrigger] = mapped_column(str_enum(RunTrigger, "trigger"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus, "status"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    records_written: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(sa.String(100))
    # Sanitized message only: never raw provider responses or headers.
    error_message: Mapped[str | None] = mapped_column(sa.String(1000))


class ServiceHealthCheck(Base):
    __tablename__ = "service_health_checks"
    __table_args__ = (sa.Index("ix_health_checks_provider_checked", "provider_id", "checked_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    monitoring_run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_runs.id", ondelete="SET NULL"), index=True
    )
    source: Mapped[HealthSource] = mapped_column(str_enum(HealthSource, "source"), nullable=False)
    status: Mapped[HealthStatus] = mapped_column(str_enum(HealthStatus, "status"), nullable=False)
    indicator: Mapped[str | None] = mapped_column(sa.String(32))  # raw provider indicator
    description: Mapped[str | None] = mapped_column(sa.String(500))
    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error_message: Mapped[str | None] = mapped_column(sa.String(500))
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_logs_occurred", "occurred_at"),
        sa.Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    actor: Mapped[Actor] = mapped_column(str_enum(Actor, "actor"), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(sa.String(64))
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    client_ip: Mapped[str | None] = mapped_column(sa.String(64))
    # Before/after values; passed through the log redactor before storage.
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONText())


class ApplicationSetting(Base):
    """Owner-editable, non-secret settings. Keys are allow-listed in SettingsService."""

    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by: Mapped[Actor] = mapped_column(str_enum(Actor, "updated_by"), nullable=False)
