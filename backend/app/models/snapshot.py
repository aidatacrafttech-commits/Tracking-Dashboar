"""Point-in-time balance and usage observations (append-only).

``amount`` / ``quantity`` are NULL when the provider could not supply a value;
``unavailable_reason`` then says why. Unknown is never recorded as zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.db.types import JSONText, ScaledDecimal, UTCDateTime
from app.models.enums import BalanceKind, UnitType, ValueSource, str_enum


class ProviderBalanceSnapshot(Base):
    __tablename__ = "provider_balance_snapshots"
    __table_args__ = (
        sa.CheckConstraint(
            "amount IS NOT NULL OR unavailable_reason IS NOT NULL",
            name="amount_or_reason",
        ),
        sa.UniqueConstraint("provider_id", "balance_kind", "source", "observed_at"),
        sa.Index("ix_balance_snapshots_provider_observed", "provider_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    monitoring_run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_runs.id", ondelete="SET NULL"), index=True
    )
    balance_kind: Mapped[BalanceKind] = mapped_column(
        str_enum(BalanceKind, "balance_kind"), nullable=False
    )
    amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    unit: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    unit_type: Mapped[UnitType] = mapped_column(str_enum(UnitType, "unit_type"), nullable=False)
    source: Mapped[ValueSource] = mapped_column(str_enum(ValueSource, "source"), nullable=False)
    # When we recorded the value vs. the point in time the provider says it reflects.
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    data_as_of: Mapped[datetime | None] = mapped_column(UTCDateTime())
    unavailable_reason: Mapped[str | None] = mapped_column(sa.String(255))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONText())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)


class ProviderUsageSnapshot(Base):
    __tablename__ = "provider_usage_snapshots"
    __table_args__ = (
        sa.CheckConstraint("period_end > period_start", name="period_order"),
        sa.CheckConstraint(
            "quantity IS NOT NULL OR cost_amount IS NOT NULL OR unavailable_reason IS NOT NULL",
            name="value_or_reason",
        ),
        sa.CheckConstraint(
            "cost_amount IS NULL OR cost_currency IS NOT NULL", name="cost_has_currency"
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity_unit IS NOT NULL", name="quantity_has_unit"
        ),
        # One row per provider/metric/dimension/period/source: re-polls update it in place
        # (observed_at shows when it was last refreshed, is_final once it stops changing).
        sa.UniqueConstraint(
            "provider_id", "metric", "dimension", "period_start", "period_end", "source"
        ),
        sa.Index("ix_usage_snapshots_provider_period", "provider_id", "period_start"),
        sa.Index("ix_usage_snapshots_provider_observed", "provider_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    monitoring_run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("monitoring_runs.id", ondelete="SET NULL"), index=True
    )
    # e.g. "cost", "input_tokens", "tts_characters", "audio_hours", "requests", "credits"
    metric: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Breakdown key such as a model or AWS service; "" means the provider total.
    dimension: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    period_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    quantity_unit: Mapped[str | None] = mapped_column(sa.String(32))
    cost_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    cost_currency: Mapped[str | None] = mapped_column(sa.String(3))
    source: Mapped[ValueSource] = mapped_column(str_enum(ValueSource, "source"), nullable=False)
    # False while the provider may still revise the figure (e.g. AWS Cost Explorer).
    is_final: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    data_as_of: Mapped[datetime | None] = mapped_column(UTCDateTime())
    unavailable_reason: Mapped[str | None] = mapped_column(sa.String(255))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONText())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
