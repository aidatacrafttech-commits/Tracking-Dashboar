"""Voucher management: Course and Live Tutor vouchers (discount coupons), what each one
applies to, the voucher holders counted against the user limit, and every redemption attempt
(the only source of usage analytics and redemption history)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utcnow
from app.db.base import Base, TimestampMixin
from app.db.types import ScaledDecimal, UTCDateTime
from app.models.enums import (
    VoucherActor,
    VoucherCategory,
    VoucherDiscountType,
    VoucherEventOutcome,
    VoucherEventSource,
    str_enum,
)


class Voucher(TimestampMixin, Base):
    __tablename__ = "vouchers"
    __table_args__ = (
        sa.CheckConstraint("max_uses IS NULL OR max_uses > 0", name="max_uses_positive"),
        sa.CheckConstraint(
            "max_uses_per_user IS NULL OR max_uses_per_user > 0", name="max_per_user_positive"
        ),
        sa.CheckConstraint("use_count >= 0", name="use_count_non_negative"),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_until IS NULL OR valid_until > valid_from",
            name="validity_window",
        ),
        sa.CheckConstraint("discount_value >= 0", name="discount_value_non_negative"),
        sa.CheckConstraint(
            "discount_type != 'percentage' OR discount_value <= 100000000",
            name="percentage_at_most_100",
        ),
        sa.CheckConstraint(
            "max_unique_users IS NULL OR max_unique_users > 0", name="max_unique_users_positive"
        ),
        sa.CheckConstraint("unique_user_count >= 0", name="unique_user_count_non_negative"),
        sa.CheckConstraint(
            "benefit_validity_days IS NULL OR benefit_validity_days > 0",
            name="benefit_days_positive",
        ),
        sa.CheckConstraint(
            "min_purchase_amount IS NULL OR min_purchase_amount >= 0",
            name="min_purchase_non_negative",
        ),
        sa.Index("ix_vouchers_created", "created_at"),
        sa.Index("ix_vouchers_category", "category", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Fixed at creation: Course and Live Tutor vouchers are managed separately.
    category: Mapped[VoucherCategory] = mapped_column(
        str_enum(VoucherCategory, "voucher_category"), nullable=False
    )
    # --- editable by the owner ----------------------------------------------------------
    # Stored upper-case; unique, so "summer25" and "SUMMER25" are the same voucher.
    code: Mapped[str] = mapped_column(sa.String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)  # internal label
    description: Mapped[str | None] = mapped_column(sa.Text)
    discount_type: Mapped[VoucherDiscountType] = mapped_column(
        str_enum(VoucherDiscountType, "discount_type"), nullable=False
    )
    # Percentage (0-100] or an amount in `currency`.
    discount_value: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    # True = every subject / every package (including ones added later); targets then empty.
    applies_to_all: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    min_purchase_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    # Days the benefit stays valid after a successful redemption (None = not applicable).
    benefit_validity_days: Mapped[int | None] = mapped_column(sa.Integer)
    max_uses: Mapped[int | None] = mapped_column(sa.Integer)  # total redemption limit
    max_uses_per_user: Mapped[int | None] = mapped_column(sa.Integer)  # None = unlimited
    max_unique_users: Mapped[int | None] = mapped_column(sa.Integer)  # None = unlimited
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    # --- system-managed (never accepted from a request body) ----------------------------
    use_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    unique_user_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_locked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    lock_reason: Mapped[str | None] = mapped_column(sa.String(255))
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[VoucherActor | None] = mapped_column(str_enum(VoucherActor, "locked_by"))
    unlocked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    unlocked_by: Mapped[VoucherActor | None] = mapped_column(str_enum(VoucherActor, "unlocked_by"))
    created_by: Mapped[VoucherActor] = mapped_column(
        str_enum(VoucherActor, "created_by"), nullable=False
    )
    updated_by: Mapped[VoucherActor] = mapped_column(
        str_enum(VoucherActor, "updated_by"), nullable=False
    )
    # Optimistic locking: a stale edit (or an edit racing a redemption) is refused.
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    targets: Mapped[list[VoucherTarget]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="VoucherTarget.id",
        passive_deletes=True,
    )

    __mapper_args__ = {"version_id_col": version}


class VoucherTarget(Base):
    """One subject (Course voucher) or package (Live Tutor voucher) a voucher applies to.
    `value_key` is the case-insensitive form used for matching and filtering."""

    __tablename__ = "voucher_targets"
    __table_args__ = (
        sa.UniqueConstraint("voucher_id", "value_key", name="uq_voucher_targets_voucher_value"),
        sa.Index("ix_voucher_targets_value_key", "value_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int] = mapped_column(
        sa.ForeignKey("vouchers.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    value_key: Mapped[str] = mapped_column(sa.String(80), nullable=False)


class VoucherUser(Base):
    """A voucher holder (e.g. a user of your app). Counted against the configurable user
    limit. Identified by your app's own id; deliberately no email or grouping fields."""

    __tablename__ = "voucher_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(sa.String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    created_by: Mapped[VoucherActor] = mapped_column(
        str_enum(VoucherActor, "created_by"), nullable=False
    )


class VoucherUsageEvent(Base):
    """One redemption attempt, successful or not. Append-only: there is no API to change or
    delete these rows. Code, user id and order details are copied so history survives
    deletions."""

    __tablename__ = "voucher_usage_events"
    __table_args__ = (
        sa.Index("ix_voucher_usage_occurred", "occurred_at"),
        sa.Index("ix_voucher_usage_voucher_time", "voucher_id", "occurred_at"),
        sa.Index("ix_voucher_usage_user", "voucher_user_id", "voucher_id", "outcome"),
        sa.Index("ix_voucher_usage_category_time", "category", "occurred_at"),
        sa.Index("ix_voucher_usage_order", "voucher_id", "order_reference"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("vouchers.id", ondelete="SET NULL")
    )
    voucher_code: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    category: Mapped[VoucherCategory | None] = mapped_column(
        str_enum(VoucherCategory, "event_category")
    )
    voucher_user_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("voucher_users.id", ondelete="SET NULL")
    )
    user_external_id: Mapped[str | None] = mapped_column(sa.String(128))
    # What was bought: a subject (course order) or a package (Live Tutor booking).
    item: Mapped[str | None] = mapped_column(sa.String(80))
    order_reference: Mapped[str | None] = mapped_column(sa.String(128))
    order_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    discount_amount: Mapped[Decimal | None] = mapped_column(ScaledDecimal())
    currency: Mapped[str | None] = mapped_column(sa.String(3))
    benefit_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    outcome: Mapped[VoucherEventOutcome] = mapped_column(
        str_enum(VoucherEventOutcome, "outcome"), nullable=False
    )
    failure_reason: Mapped[str | None] = mapped_column(sa.String(32))
    source: Mapped[VoucherEventSource] = mapped_column(
        str_enum(VoucherEventSource, "source"), nullable=False
    )
    actor: Mapped[VoucherActor] = mapped_column(str_enum(VoucherActor, "actor"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
