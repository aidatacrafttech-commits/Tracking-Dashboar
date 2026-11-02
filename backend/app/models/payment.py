from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import ScaledDecimal, UTCDateTime
from app.models.enums import Actor, EntryMethod, PaymentKind, str_enum


class PaymentTransaction(TimestampMixin, Base):
    """Money paid to a provider. Amounts are always positive; ``kind`` gives direction."""

    __tablename__ = "payment_transactions"
    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="amount_positive"),
        sa.CheckConstraint("length(currency) = 3", name="currency_iso"),
        sa.UniqueConstraint("provider_id", "payment_reference"),
        sa.UniqueConstraint("provider_id", "external_id"),
        sa.Index("ix_payments_provider_date", "provider_id", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        sa.ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[PaymentKind] = mapped_column(str_enum(PaymentKind, "kind"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    transaction_date: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # Owner-supplied reference (receipt/invoice number). NULLs are allowed to repeat.
    payment_reference: Mapped[str | None] = mapped_column(sa.String(128))
    # Provider's own id (e.g. Deepgram order_id) for provider-verified records.
    external_id: Mapped[str | None] = mapped_column(sa.String(128))
    entry_method: Mapped[EntryMethod] = mapped_column(
        str_enum(EntryMethod, "entry_method"), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_by: Mapped[Actor] = mapped_column(str_enum(Actor, "created_by"), nullable=False)
    # A payment never changes a balance by itself. Only an explicit owner reconciliation
    # creates a new manual balance snapshot; these record that it happened.
    reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reconciled_balance_snapshot_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("provider_balance_snapshots.id", ondelete="SET NULL")
    )
