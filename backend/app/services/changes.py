"""Change detection for the Server-Sent Events stream.

The stream carries topic names only (no data, no secrets). The browser refetches the
affected queries through the normal authenticated API. Markers are cheap aggregate
queries, so this works without any in-process event bus and across restarts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AlertDeliveryAttempt,
    AlertEvent,
    AuditLog,
    InfrastructureResourceSnapshot,
    PaymentTransaction,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ProviderUsageSnapshot,
    ServiceHealthCheck,
    ThresholdConfig,
)

TOPICS = ("balances", "usage", "sync", "alerts", "payments", "thresholds", "resources", "health")


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def change_markers(session_factory: sessionmaker[Session]) -> dict[str, str]:
    session = session_factory()
    try:

        def one(*cols: Any) -> str:
            # One independent scalar subquery per aggregate: no cross join between tables.
            row = session.execute(select(*(select(c).scalar_subquery() for c in cols))).one()
            return "|".join(_str(v) for v in row)

        return {
            "balances": one(func.max(ProviderBalanceSnapshot.id)),
            "usage": one(
                func.max(ProviderUsageSnapshot.observed_at), func.count(ProviderUsageSnapshot.id)
            ),
            "sync": one(func.max(ProviderSyncState.updated_at)),
            "alerts": one(
                func.max(AlertEvent.id),
                func.max(AlertDeliveryAttempt.id),
                func.count(AlertEvent.id),
            ),
            # Audit rows capture edits and deletions that leave no other trace.
            "payments": one(
                func.max(PaymentTransaction.updated_at),
                func.count(PaymentTransaction.id),
                func.max(AuditLog.id),
            ),
            "thresholds": one(
                func.max(ThresholdConfig.updated_at), func.max(ThresholdConfig.version)
            ),
            "resources": one(func.max(InfrastructureResourceSnapshot.id)),
            "health": one(func.max(ServiceHealthCheck.id)),
        }
    finally:
        session.close()


def changed_topics(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [t for t in TOPICS if before.get(t) != after.get(t)]
