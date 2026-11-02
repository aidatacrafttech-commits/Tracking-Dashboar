from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import BigInteger, case, func, or_, select, type_coerce
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.money import from_storage_int, strip_trailing_zeros
from app.core.time import utcnow
from app.db.session import transaction
from app.models import PaymentTransaction, Provider, ProviderBalanceSnapshot
from app.models.enums import (
    BalanceCapability,
    BalanceKind,
    EntryMethod,
    PaymentKind,
    UnitType,
    ValueSource,
)
from app.repositories.audit import AuditLogRepository
from app.repositories.payments import PaymentRepository
from app.schemas.payment import (
    CurrencyTotal,
    PaymentCreate,
    PaymentPage,
    PaymentRead,
    ReconcileBase,
    ReconcilePreview,
)
from app.schemas.snapshot import BalanceSnapshotRead
from app.services.context import RequestContext
from app.services.providers import ProviderService

# Allow small clock skew between the owner's device and the server.
FUTURE_TOLERANCE = timedelta(minutes=5)
# Providers whose balance is maintained by hand; only these can be reconciled from a
# payment. Balances reported by a provider API are never overridden by payment records.
RECONCILABLE = frozenset({BalanceCapability.MANUAL_ONLY, BalanceCapability.PARTIAL})


def to_payment_read(payment: PaymentTransaction, provider: Provider) -> PaymentRead:
    return PaymentRead(
        id=payment.id,
        provider=provider.slug,
        kind=payment.kind,
        amount=payment.amount,
        currency=payment.currency,
        transaction_date=payment.transaction_date,
        payment_reference=payment.payment_reference,
        external_id=payment.external_id,
        entry_method=payment.entry_method,
        verified_at=payment.verified_at,
        notes=payment.notes,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
        reconciled_at=payment.reconciled_at,
        reconciled_balance_snapshot_id=payment.reconciled_balance_snapshot_id,
        editable=payment.entry_method is EntryMethod.MANUAL,
    )


def _audit_view(payment: PaymentTransaction, slug: str) -> dict[str, Any]:
    return {
        "provider": slug,
        "kind": payment.kind.value,
        "amount": str(strip_trailing_zeros(payment.amount)),  # consistent before/after
        "currency": payment.currency,
        "transaction_date": payment.transaction_date.isoformat(),
        "payment_reference": payment.payment_reference,
        "notes": payment.notes,
    }


class PaymentService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.providers = ProviderService(session)
        self.payments = PaymentRepository(session)
        self.audit = AuditLogRepository(session)

    # --- reads -----------------------------------------------------------------------------
    def list_payments(
        self,
        slug: str,
        *,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> PaymentPage:
        return self.search(
            slug=slug,
            currency=None,
            start=start,
            end=end,
            q=None,
            sort="date",
            order="desc",
            limit=limit,
            offset=offset,
        )

    def search(
        self,
        *,
        slug: str | None,
        currency: str | None,
        start: datetime | None,
        end: datetime | None,
        q: str | None,
        sort: Literal["date", "amount", "provider"],
        order: Literal["asc", "desc"],
        limit: int,
        offset: int,
    ) -> PaymentPage:
        if start is not None and end is not None and end <= start:
            raise DomainValidationError("'end' must be after 'start'", code="invalid_range")
        P = PaymentTransaction
        query = select(P, Provider).join(Provider, Provider.id == P.provider_id)
        if slug is not None:
            query = query.where(P.provider_id == self.providers.require_provider(slug).id)
        if currency:
            query = query.where(P.currency == currency.upper())
        if start is not None:
            query = query.where(P.transaction_date >= start)
        if end is not None:
            query = query.where(P.transaction_date < end)
        if q and any(ord(ch) < 32 or ord(ch) == 127 for ch in q):
            # e.g. NUL would truncate the LIKE pattern inside SQLite and match everything
            raise DomainValidationError(
                "Search text contains control characters", code="invalid_search"
            )
        if q:
            # Bound parameter (no SQL injection) with LIKE wildcards escaped, so "%" or "_"
            # in the search text match literally instead of everything.
            needle = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{needle}%"
            query = query.where(
                or_(
                    func.lower(func.coalesce(P.payment_reference, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(P.notes, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(P.external_id, "")).like(pattern, escape="\\"),
                    func.lower(Provider.display_name).like(pattern, escape="\\"),
                )
            )
        column = {
            "date": P.transaction_date,
            "amount": type_coerce(P.amount, BigInteger),
            "provider": Provider.display_name,
        }[sort]
        ordering = (column.asc() if order == "asc" else column.desc(), P.id.desc())
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.execute(query.order_by(*ordering).limit(limit).offset(offset)).all()

        filtered = query.subquery()
        raw = type_coerce(filtered.c.amount, BigInteger)
        signed = case((filtered.c.kind == PaymentKind.REFUND.value, -raw), else_=raw)
        totals = self.session.execute(
            select(filtered.c.currency, func.sum(signed), func.count())
            .group_by(filtered.c.currency)
            .order_by(filtered.c.currency)
        ).all()
        return PaymentPage(
            items=[to_payment_read(p, prov) for p, prov in rows],
            total=total,
            limit=limit,
            offset=offset,
            totals=[
                CurrencyTotal(currency=c, net_amount=from_storage_int(int(n)), count=int(k))
                for c, n, k in totals
            ],
        )

    def _require(self, payment_id: int) -> tuple[PaymentTransaction, Provider]:
        row = self.session.execute(
            select(PaymentTransaction, Provider)
            .join(Provider, Provider.id == PaymentTransaction.provider_id)
            .where(PaymentTransaction.id == payment_id)
        ).first()
        if row is None:
            raise NotFoundError(f"Payment {payment_id} not found", code="payment_not_found")
        return row[0], row[1]

    def get(self, payment_id: int) -> PaymentRead:
        payment, provider = self._require(payment_id)
        return to_payment_read(payment, provider)

    # --- validation ---------------------------------------------------------------------------
    def _validate(self, body: PaymentCreate) -> None:
        if body.currency not in self.settings.supported_currencies:
            raise DomainValidationError(
                f"Currency '{body.currency}' is not supported",
                code="unsupported_currency",
                details={"supported": self.settings.supported_currencies},
            )
        if body.transaction_date > utcnow() + FUTURE_TOLERANCE:
            raise DomainValidationError(
                "transaction_date cannot be in the future", code="future_transaction_date"
            )

    def _check_reference(
        self, provider_id: int, reference: str | None, exclude: int | None
    ) -> None:
        if not reference:
            return
        query = select(PaymentTransaction.id).where(
            PaymentTransaction.provider_id == provider_id,
            PaymentTransaction.payment_reference == reference,
        )
        if exclude is not None:
            query = query.where(PaymentTransaction.id != exclude)
        if self.session.scalar(query) is not None:
            raise ConflictError(
                "A payment with this reference already exists for this provider",
                code="duplicate_payment_reference",
            )

    @staticmethod
    def _require_manual(payment: PaymentTransaction) -> None:
        if payment.entry_method is not EntryMethod.MANUAL:
            raise ConflictError(
                "Provider-verified payments are read-only", code="payment_not_editable"
            )

    # --- mutations ------------------------------------------------------------------------------
    def create_manual_payment(
        self, slug: str, body: PaymentCreate, ctx: RequestContext
    ) -> PaymentRead:
        """Record an owner-entered payment. It does NOT change any balance by itself."""
        self._validate(body)
        provider = self.providers.require_provider(slug)
        with transaction(self.session):
            self._check_reference(provider.id, body.payment_reference, None)
            payment = self.payments.add(
                PaymentTransaction(
                    provider_id=provider.id,
                    kind=body.kind,
                    amount=body.amount,
                    currency=body.currency,
                    transaction_date=body.transaction_date,
                    payment_reference=body.payment_reference,
                    entry_method=EntryMethod.MANUAL,
                    notes=body.notes,
                    created_by=ctx.actor,
                )
            )
            self.audit.record(
                actor=ctx.actor,
                action="payment.create",
                entity_type="payment_transaction",
                entity_id=payment.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={"after": _audit_view(payment, provider.slug)},
            )
        return to_payment_read(payment, provider)

    def update_manual_payment(
        self, payment_id: int, body: PaymentCreate, ctx: RequestContext
    ) -> PaymentRead:
        self._validate(body)
        with transaction(self.session):
            payment, provider = self._require(payment_id)
            self._require_manual(payment)
            if payment.reconciled_at is not None and (
                body.amount != payment.amount
                or body.currency != payment.currency
                or body.kind is not payment.kind
            ):
                raise ConflictError(
                    "This payment was already applied to a balance; its amount, currency and "
                    "kind can no longer change. Record a correcting balance entry instead.",
                    code="payment_reconciled",
                )
            self._check_reference(provider.id, body.payment_reference, payment.id)
            before = _audit_view(payment, provider.slug)
            payment.kind = body.kind
            payment.amount = body.amount
            payment.currency = body.currency
            payment.transaction_date = body.transaction_date
            payment.payment_reference = body.payment_reference
            payment.notes = body.notes
            self.session.flush()
            self.audit.record(
                actor=ctx.actor,
                action="payment.update",
                entity_type="payment_transaction",
                entity_id=payment.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={"before": before, "after": _audit_view(payment, provider.slug)},
            )
        return to_payment_read(payment, provider)

    def delete_manual_payment(self, payment_id: int, ctx: RequestContext) -> None:
        with transaction(self.session):
            payment, provider = self._require(payment_id)
            self._require_manual(payment)
            # The full record goes into the audit log, so a deletion is never silent.
            self.audit.record(
                actor=ctx.actor,
                action="payment.delete",
                entity_type="payment_transaction",
                entity_id=payment.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={
                    "before": {
                        **_audit_view(payment, provider.slug),
                        "reconciled": payment.reconciled_at is not None,
                    }
                },
            )
            self.session.delete(payment)

    # --- explicit reconciliation ------------------------------------------------------------
    def _latest_balance(self, provider: Provider, unit: str) -> ProviderBalanceSnapshot | None:
        return self.session.scalar(
            select(ProviderBalanceSnapshot)
            .where(
                ProviderBalanceSnapshot.provider_id == provider.id,
                ProviderBalanceSnapshot.balance_kind == BalanceKind.PREPAID_BALANCE,
                ProviderBalanceSnapshot.unit == unit,
                ProviderBalanceSnapshot.amount.is_not(None),
            )
            .order_by(ProviderBalanceSnapshot.observed_at.desc(), ProviderBalanceSnapshot.id.desc())
            .limit(1)
        )

    def reconcile_preview(self, payment_id: int) -> ReconcilePreview:
        payment, provider = self._require(payment_id)

        def refuse(reason: str, base: ProviderBalanceSnapshot | None = None) -> ReconcilePreview:
            return ReconcilePreview(
                payment_id=payment.id,
                allowed=False,
                reason=reason,
                base=self._base(base),
                payment_effect=None,
                resulting_balance=None,
                unit=payment.currency,
            )

        if payment.reconciled_at is not None:
            return refuse("this payment was already applied to a balance")
        if provider.balance_capability not in RECONCILABLE:
            return refuse(
                f"{provider.display_name}'s balance comes from its provider API or billing data; "
                "payments are not applied to it by hand"
            )
        base = self._latest_balance(provider, payment.currency)
        if base is None or base.amount is None:
            return refuse(
                f"no {payment.currency} prepaid balance recorded yet; enter the current balance "
                "manually instead"
            )
        if payment.transaction_date <= base.observed_at:
            return refuse(
                "the latest balance was observed after this payment, so it may already include it",
                base,
            )
        effect = -payment.amount if payment.kind is PaymentKind.REFUND else payment.amount
        return ReconcilePreview(
            payment_id=payment.id,
            allowed=True,
            reason=None,
            base=self._base(base),
            payment_effect=effect,
            resulting_balance=base.amount + effect,
            unit=payment.currency,
        )

    @staticmethod
    def _base(snapshot: ProviderBalanceSnapshot | None) -> ReconcileBase | None:
        if snapshot is None or snapshot.amount is None:
            return None
        return ReconcileBase(
            snapshot_id=snapshot.id,
            amount=snapshot.amount,
            unit=snapshot.unit,
            source=snapshot.source.value,
            observed_at=snapshot.observed_at,
        )

    def reconcile(
        self, payment_id: int, base_snapshot_id: int, ctx: RequestContext
    ) -> BalanceSnapshotRead:
        """Owner-confirmed: new manual balance = reviewed base balance +/- this payment."""
        with transaction(self.session):
            preview = self.reconcile_preview(payment_id)
            if not preview.allowed or preview.base is None or preview.resulting_balance is None:
                raise ConflictError(
                    preview.reason or "cannot reconcile", code="reconcile_not_allowed"
                )
            if preview.base.snapshot_id != base_snapshot_id:
                raise ConflictError(
                    "The balance changed since you reviewed it; review the new preview",
                    code="balance_changed",
                )
            payment, provider = self._require(payment_id)
            snapshot = ProviderBalanceSnapshot(
                provider_id=provider.id,
                balance_kind=BalanceKind.PREPAID_BALANCE,
                amount=preview.resulting_balance,
                unit=payment.currency,
                unit_type=UnitType.CURRENCY,
                source=ValueSource.MANUAL,
                observed_at=utcnow(),
                details={
                    "reconciliation": {
                        "payment_id": payment.id,
                        "base_snapshot_id": preview.base.snapshot_id,
                        "base_amount": str(preview.base.amount),
                        "payment_effect": str(preview.payment_effect),
                    }
                },
            )
            self.session.add(snapshot)
            self.session.flush()
            payment.reconciled_at = snapshot.observed_at
            payment.reconciled_balance_snapshot_id = snapshot.id
            self.audit.record(
                actor=ctx.actor,
                action="payment.reconcile",
                entity_type="payment_transaction",
                entity_id=payment.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={
                    "base_amount": str(preview.base.amount),
                    "payment_effect": str(preview.payment_effect),
                    "new_balance": str(preview.resulting_balance),
                    "balance_snapshot_id": snapshot.id,
                },
            )
        from app.services.alerts import AlertEvaluator

        AlertEvaluator(self.session, self.settings).evaluate_provider(provider.slug)
        return BalanceSnapshotRead.model_validate(snapshot)
