from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Select, case, func, select, type_coerce
from sqlalchemy.orm import Session

from app.core.money import from_storage_int
from app.models import PaymentTransaction
from app.models.enums import PaymentKind


class PaymentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, payment: PaymentTransaction) -> PaymentTransaction:
        self.session.add(payment)
        self.session.flush()
        return payment

    def _filtered(self, provider_id: int, start: datetime | None, end: datetime | None) -> Select:
        query = select(PaymentTransaction).where(PaymentTransaction.provider_id == provider_id)
        if start is not None:
            query = query.where(PaymentTransaction.transaction_date >= start)
        if end is not None:
            query = query.where(PaymentTransaction.transaction_date < end)
        return query

    def totals_by_currency(
        self, provider_id: int, *, start: datetime | None, end: datetime | None
    ) -> list[tuple[str, Decimal, int]]:
        """Net paid per currency (refunds subtract). Summed as exact integers in SQL."""
        filtered = self._filtered(provider_id, start, end).subquery()
        # Sum the raw stored micro-units (exact integers), converting once at the end.
        raw_amount = type_coerce(filtered.c.amount, BigInteger)
        signed = case((filtered.c.kind == PaymentKind.REFUND.value, -raw_amount), else_=raw_amount)
        rows = self.session.execute(
            select(filtered.c.currency, func.sum(signed), func.count())
            .group_by(filtered.c.currency)
            .order_by(filtered.c.currency)
        ).all()
        return [(currency, from_storage_int(int(net)), int(count)) for currency, net, count in rows]
