from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import ProviderBalanceSnapshot, ProviderUsageSnapshot


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_balance(self, snapshot: ProviderBalanceSnapshot) -> ProviderBalanceSnapshot:
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def latest_balances(self, provider_ids: list[int]) -> dict[int, ProviderBalanceSnapshot]:
        """Most recent balance snapshot per provider (one query)."""
        if not provider_ids:
            return {}
        ranked = (
            select(
                ProviderBalanceSnapshot.id,
                func.row_number()
                .over(
                    partition_by=ProviderBalanceSnapshot.provider_id,
                    order_by=(
                        ProviderBalanceSnapshot.observed_at.desc(),
                        ProviderBalanceSnapshot.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .where(ProviderBalanceSnapshot.provider_id.in_(provider_ids))
            .subquery()
        )
        rows = self.session.scalars(
            select(ProviderBalanceSnapshot).join(
                ranked, (ranked.c.id == ProviderBalanceSnapshot.id) & (ranked.c.rn == 1)
            )
        )
        return {row.provider_id: row for row in rows}

    def list_balances(
        self,
        provider_id: int,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ProviderBalanceSnapshot], int]:
        model = ProviderBalanceSnapshot
        query = select(model).where(model.provider_id == provider_id)
        if start is not None:
            query = query.where(model.observed_at >= start)
        if end is not None:
            query = query.where(model.observed_at < end)
        return self._page(query, (model.observed_at.desc(), model.id.desc()), limit, offset)

    def list_usage(
        self,
        provider_id: int,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ProviderUsageSnapshot], int]:
        model = ProviderUsageSnapshot
        query = select(model).where(model.provider_id == provider_id)
        # Usage is filtered by the period it describes, not when we fetched it.
        if start is not None:
            query = query.where(model.period_end > start)
        if end is not None:
            query = query.where(model.period_start < end)
        return self._page(
            query,
            (model.period_start.desc(), model.observed_at.desc(), model.id.desc()),
            limit,
            offset,
        )

    def _page(self, query: Select, order_by: tuple, limit: int, offset: int) -> tuple[list, int]:
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        items = list(self.session.scalars(query.order_by(*order_by).limit(limit).offset(offset)))
        return items, total
