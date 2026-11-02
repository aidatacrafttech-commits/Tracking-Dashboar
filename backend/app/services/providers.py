from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.core.time import utcnow
from app.models import Provider, ProviderBalanceSnapshot, ThresholdConfig
from app.models.enums import BalanceCapability
from app.repositories.providers import ProviderRepository
from app.repositories.snapshots import SnapshotRepository
from app.repositories.thresholds import ThresholdRepository
from app.schemas.provider import ProviderRead, ThresholdHeadroom
from app.schemas.snapshot import (
    BalanceSnapshotPage,
    BalanceSnapshotRead,
    Freshness,
    UsageSnapshotPage,
    UsageSnapshotRead,
    source_type_for,
)
from app.schemas.sync import CapabilityRead, IntegrationRead
from app.schemas.threshold import ThresholdSummary

if TYPE_CHECKING:
    from app.providers.registry import ProviderRuntime

CAPABILITY_NOTES: dict[BalanceCapability, str] = {
    BalanceCapability.API: "Actual balance is reported by the provider API.",
    BalanceCapability.PARTIAL: (
        "The provider API reports plan quota only; prepaid top-up balance must be entered manually."
    ),
    BalanceCapability.MANUAL_ONLY: (
        "The provider has no balance API. Balance is entered manually or estimated "
        "from usage since the last manual entry."
    ),
    BalanceCapability.POSTPAID: (
        "Billed in arrears. Remaining credits and spend come from billing data that "
        "can lag by up to 24 hours."
    ),
}


def build_freshness(snapshot: ProviderBalanceSnapshot, stale_after_minutes: int) -> Freshness:
    reference = snapshot.data_as_of or snapshot.observed_at
    age = max(0, int((utcnow() - reference).total_seconds()))
    return Freshness(
        observed_at=snapshot.observed_at,
        data_as_of=snapshot.data_as_of,
        age_seconds=age,
        is_stale=age > stale_after_minutes * 60,
    )


def integration_read(runtime: ProviderRuntime | None) -> IntegrationRead | None:
    if runtime is None:
        return None
    return IntegrationRead(
        status=runtime.status,
        missing_config=list(runtime.missing_config),
        note=runtime.note,
        capabilities=[
            CapabilityRead(operation=op, supported=info.supported, note=info.note)
            for op, info in runtime.capabilities.items()
        ],
    )


class ProviderService:
    def __init__(
        self, session: Session, runtimes: dict[str, ProviderRuntime] | None = None
    ) -> None:
        self.runtimes = runtimes or {}
        self.providers = ProviderRepository(session)
        self.snapshots = SnapshotRepository(session)
        self.thresholds = ThresholdRepository(session)

    def require_provider(self, slug: str) -> Provider:
        provider = self.providers.get_by_slug(slug)
        if provider is None:
            raise NotFoundError(f"Provider '{slug}' not found", code="provider_not_found")
        return provider

    def list_providers(self) -> list[ProviderRead]:
        providers = self.providers.list_all()
        ids = [p.id for p in providers]
        latest = self.snapshots.latest_balances(ids)
        thresholds = self.thresholds.for_providers(ids)
        return [
            self._with_headroom(
                self._to_read(p, latest.get(p.id), thresholds.get(p.id), self.runtimes.get(p.slug)),
                p,
                thresholds.get(p.id),
            )
            for p in providers
        ]

    def get_provider(self, slug: str) -> ProviderRead:
        provider = self.require_provider(slug)
        latest = self.snapshots.latest_balances([provider.id]).get(provider.id)
        threshold = self.thresholds.get_for_provider(provider.id)
        read = self._to_read(provider, latest, threshold, self.runtimes.get(provider.slug))
        return self._with_headroom(read, provider, threshold)

    def _with_headroom(
        self, read: ProviderRead, provider: Provider, threshold: ThresholdConfig | None
    ) -> ProviderRead:
        if threshold is None:
            return read
        snap = self.snapshots.session.scalar(
            select(ProviderBalanceSnapshot)
            .where(
                ProviderBalanceSnapshot.provider_id == provider.id,
                ProviderBalanceSnapshot.balance_kind == threshold.balance_kind,
                ProviderBalanceSnapshot.unit == threshold.unit,
                ProviderBalanceSnapshot.amount.is_not(None),
            )
            .order_by(ProviderBalanceSnapshot.observed_at.desc(), ProviderBalanceSnapshot.id.desc())
            .limit(1)
        )
        if snap is None or snap.amount is None:
            return read
        diff = snap.amount - threshold.amount
        freshness = build_freshness(snap, provider.stale_after_minutes)
        read.threshold_headroom = ThresholdHeadroom(
            amount=diff,
            unit=threshold.unit,
            position="above" if diff > 0 else "at" if diff == 0 else "below",
            balance_source_type=source_type_for(snap.source, True),
            is_stale=freshness.is_stale,
        )
        return read

    def list_snapshots(
        self,
        slug: str,
        *,
        kind: Literal["balance", "usage"],
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> BalanceSnapshotPage | UsageSnapshotPage:
        if start is not None and end is not None and end <= start:
            raise DomainValidationError("'end' must be after 'start'", code="invalid_range")
        if start is not None and end is not None and end - start > timedelta(days=800):
            raise DomainValidationError("Range may not exceed 800 days", code="invalid_range")
        provider = self.require_provider(slug)
        if kind == "balance":
            items, total = self.snapshots.list_balances(
                provider.id, start=start, end=end, limit=limit, offset=offset
            )
            return BalanceSnapshotPage(
                items=[BalanceSnapshotRead.model_validate(i) for i in items],
                total=total,
                limit=limit,
                offset=offset,
            )
        usage, total = self.snapshots.list_usage(
            provider.id, start=start, end=end, limit=limit, offset=offset
        )
        return UsageSnapshotPage(
            items=[UsageSnapshotRead.model_validate(i) for i in usage],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _to_read(
        provider: Provider,
        latest: ProviderBalanceSnapshot | None,
        threshold: ThresholdConfig | None,
        runtime: ProviderRuntime | None = None,
    ) -> ProviderRead:
        return ProviderRead(
            id=provider.slug,
            display_name=provider.display_name,
            balance_capability=provider.balance_capability,
            balance_capability_note=CAPABILITY_NOTES[provider.balance_capability],
            default_unit=provider.default_unit,
            default_unit_type=provider.default_unit_type,
            stale_after_minutes=provider.stale_after_minutes,
            status_page_url=provider.status_page_url,
            is_enabled=provider.is_enabled,
            latest_balance=BalanceSnapshotRead.model_validate(latest) if latest else None,
            latest_balance_freshness=(
                build_freshness(latest, provider.stale_after_minutes) if latest else None
            ),
            threshold=ThresholdSummary.model_validate(threshold) if threshold else None,
            integration=integration_read(runtime),
        )
