from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import BalanceCapability, UnitType, str_enum


class Provider(TimestampMixin, Base):
    __tablename__ = "providers"
    __table_args__ = (
        sa.CheckConstraint("stale_after_minutes > 0", name="stale_after_positive"),
        sa.CheckConstraint("slug = lower(slug)", name="slug_lowercase"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    balance_capability: Mapped[BalanceCapability] = mapped_column(
        str_enum(BalanceCapability, "balance_capability"), nullable=False
    )
    default_unit: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    default_unit_type: Mapped[UnitType] = mapped_column(
        str_enum(UnitType, "default_unit_type"), nullable=False
    )
    # A balance/usage value older than this is shown as stale.
    stale_after_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status_page_url: Mapped[str | None] = mapped_column(sa.String(255))
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(sa.Text)
