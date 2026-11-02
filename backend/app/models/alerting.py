from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base, TimestampMixin
from app.db.types import JSONText, ScaledDecimal, UTCDateTime
from app.models.enums import (
    AlertEventType,
    AlertState,
    AttemptOutcome,
    BalanceKind,
    DeliveryStatus,
    EvaluationStatus,
    UnitType,
    str_enum,
)

DEFAULT_ELIGIBLE_SOURCES = ("api_verified", "manual")


class ThresholdConfig(TimestampMixin, Base):
    """One low-balance threshold per provider, plus its persisted alert state.

    Alert fires when balance <= amount; recovers when balance > amount + recovery_margin
    (hysteresis prevents flapping around the threshold).
    """

    __tablename__ = "threshold_configs"
    __table_args__ = (
        sa.CheckConstraint("amount >= 0", name="amount_non_negative"),
        sa.CheckConstraint("recovery_margin >= 0", name="recovery_margin_non_negative"),
        sa.CheckConstraint(
            "cooldown_minutes IS NULL OR cooldown_minutes >= 15", name="cooldown_min"
        ),
        sa.CheckConstraint(
            "monitoring_interval_minutes IS NULL OR monitoring_interval_minutes >= 5",
            name="monitoring_interval_min",
        ),
        sa.CheckConstraint("breach_episode >= 0", name="episode_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    balance_kind: Mapped[BalanceKind] = mapped_column(
        str_enum(BalanceKind, "balance_kind"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    unit_type: Mapped[UnitType] = mapped_column(str_enum(UnitType, "unit_type"), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    notify_on_breach: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    notify_on_recovery: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    recovery_margin: Mapped[Decimal] = mapped_column(
        ScaledDecimal(), nullable=False, default=Decimal(0)
    )
    # Minimum gap between repeat low-balance emails while still breached. NULL = one
    # email per breach; the next one only after recovery and a new crossing.
    cooldown_minutes: Mapped[int | None] = mapped_column(sa.Integer)
    # NULL means "use ALERT_RECIPIENTS from the environment".
    notification_emails: Mapped[list[str] | None] = mapped_column(JSONText())
    # Which balance sources may trigger alerts: api_verified / manual / usage_estimate.
    eligible_sources: Mapped[list[str]] = mapped_column(
        JSONText(), nullable=False, default=lambda: list(DEFAULT_ELIGIBLE_SOURCES)
    )
    # Per-provider balance polling interval; NULL = POLL_INTERVAL_MINUTES.
    monitoring_interval_minutes: Mapped[int | None] = mapped_column(sa.Integer)

    alert_state: Mapped[AlertState] = mapped_column(
        str_enum(AlertState, "alert_state"), nullable=False, default=AlertState.UNKNOWN
    )
    state_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # When the last alert event (breach/reminder) was created; drives the cooldown.
    last_notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_balance_snapshot_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("provider_balance_snapshots.id", ondelete="SET NULL")
    )
    # Incremented on every transition into BREACHED; part of each alert's dedupe key.
    breach_episode: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_evaluation_status: Mapped[EvaluationStatus | None] = mapped_column(
        str_enum(EvaluationStatus, "last_evaluation_status")
    )
    last_evaluation_message: Mapped[str | None] = mapped_column(sa.String(255))
    # Optimistic locking: concurrent evaluator/owner updates fail instead of clobbering.
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class AlertEvent(Base):
    """An alert and its email, stored together (transactional outbox)."""

    __tablename__ = "alert_events"
    __table_args__ = (
        sa.CheckConstraint("delivery_attempts >= 0", name="attempts_non_negative"),
        sa.Index("ix_alert_events_provider_occurred", "provider_id", "occurred_at"),
        sa.Index("ix_alert_events_delivery_status", "delivery_status"),
        sa.Index("ix_alert_events_due", "delivery_status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    threshold_config_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("threshold_configs.id", ondelete="SET NULL")
    )
    balance_snapshot_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("provider_balance_snapshots.id", ondelete="SET NULL")
    )
    event_type: Mapped[AlertEventType] = mapped_column(
        str_enum(AlertEventType, "event_type"), nullable=False
    )
    observed_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    threshold_amount: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="email")
    recipients: Mapped[list[str] | None] = mapped_column(JSONText())
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    delivery_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.String(500))
    # One logical alert = one row, e.g. "t3:e2:breached" (threshold 3, breach episode 2).
    # The UNIQUE constraint makes duplicate creation impossible, even across workers.
    dedupe_key: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    episode: Mapped[int | None] = mapped_column(sa.Integer)
    # The rendered message is stored in the same transaction as the state change, so a
    # crash between commit and send cannot lose the alert.
    subject: Mapped[str | None] = mapped_column(sa.String(255))
    body_text: Mapped[str | None] = mapped_column(sa.Text)
    body_html: Mapped[str | None] = mapped_column(sa.Text)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONText())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)


class AlertDeliveryAttempt(Base):
    """Every delivery attempt and its outcome (append-only)."""

    __tablename__ = "alert_delivery_attempts"
    __table_args__ = (
        sa.UniqueConstraint("alert_event_id", "attempt_number"),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_event_id: Mapped[int] = mapped_column(
        sa.ForeignKey("alert_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    outcome: Mapped[AttemptOutcome] = mapped_column(
        str_enum(AttemptOutcome, "outcome"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(32))
    error_message: Mapped[str | None] = mapped_column(sa.String(500))
    smtp_status: Mapped[int | None] = mapped_column(sa.Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
