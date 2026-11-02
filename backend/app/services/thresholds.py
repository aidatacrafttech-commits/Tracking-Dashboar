from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.money import decimal_places, minor_units_for
from app.core.time import utcnow
from app.db.session import transaction
from app.models import Provider, ThresholdConfig
from app.models.enums import AlertState, BalanceCapability, BalanceKind, UnitType
from app.repositories.audit import AuditLogRepository
from app.repositories.thresholds import ThresholdRepository
from app.schemas.threshold import ThresholdRead, ThresholdUpsert
from app.services.alerts import AlertEvaluator
from app.services.context import RequestContext
from app.services.providers import ProviderService

# Which balance a threshold can watch, given what the provider exposes (Phase 0 matrix).
ALLOWED_BALANCE_KINDS: dict[BalanceCapability, tuple[BalanceKind, ...]] = {
    BalanceCapability.API: (BalanceKind.PREPAID_BALANCE,),
    BalanceCapability.MANUAL_ONLY: (BalanceKind.PREPAID_BALANCE,),
    BalanceCapability.PARTIAL: (BalanceKind.PLAN_QUOTA,),
    BalanceCapability.POSTPAID: (BalanceKind.PROMOTIONAL_CREDIT,),
}

# Changing any of these invalidates the current alert state until the next evaluation.
_STATE_RESET_FIELDS = ("amount", "unit", "balance_kind", "is_enabled", "eligible_sources")

_AUDITED_FIELDS = (
    "balance_kind",
    "amount",
    "unit",
    "is_enabled",
    "notify_on_breach",
    "notify_on_recovery",
    "recovery_margin",
    "cooldown_minutes",
    "notification_emails",
    "eligible_sources",
    "monitoring_interval_minutes",
)


_DERIVED = {"provider", "recovery_boundary", "effective_recipients", "email_delivery_enabled"}


def to_threshold_read(
    threshold: ThresholdConfig, provider: Provider, settings: Settings
) -> ThresholdRead:
    return ThresholdRead.model_validate(
        {
            **{c: getattr(threshold, c) for c in ThresholdRead.model_fields if c not in _DERIVED},
            "provider": provider.slug,
            "recovery_boundary": threshold.amount + threshold.recovery_margin,
            "effective_recipients": list(
                threshold.notification_emails or settings.alert_recipients
            ),
            "email_delivery_enabled": settings.email_delivery_enabled,
        }
    )


def _snapshot(threshold: ThresholdConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in _AUDITED_FIELDS:
        value = getattr(threshold, field)
        out[field] = value if value is None or isinstance(value, bool | int | list) else str(value)
    return out


class ThresholdService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.providers = ProviderService(session)
        self.thresholds = ThresholdRepository(session)
        self.audit = AuditLogRepository(session)

    def get_threshold(self, slug: str) -> ThresholdRead:
        provider = self.providers.require_provider(slug)
        threshold = self.thresholds.get_for_provider(provider.id)
        if threshold is None:
            raise NotFoundError(
                f"No threshold configured for '{slug}'", code="threshold_not_configured"
            )
        return to_threshold_read(threshold, provider, self.settings)

    def _validate(self, provider: Provider, body: ThresholdUpsert) -> BalanceKind:
        if body.unit != provider.default_unit:
            raise DomainValidationError(
                f"Threshold unit must be {provider.default_unit} for {provider.display_name}",
                code="unit_mismatch",
                details={"expected_unit": provider.default_unit},
            )
        if body.unit_type is UnitType.CURRENCY:
            if body.unit not in self.settings.supported_currencies:
                raise DomainValidationError(
                    f"Currency '{body.unit}' is not supported",
                    code="unsupported_currency",
                    details={"supported": self.settings.supported_currencies},
                )
            places = minor_units_for(body.unit)
            for field in ("amount", "recovery_margin"):
                if decimal_places(getattr(body, field)) > places:
                    raise DomainValidationError(
                        f"{field} must have at most {places} decimal places for {body.unit}",
                        code="invalid_precision",
                    )

        allowed = ALLOWED_BALANCE_KINDS[provider.balance_capability]
        kind = body.balance_kind or allowed[0]
        if kind not in allowed:
            raise DomainValidationError(
                f"{provider.display_name} supports balance_kind: "
                + ", ".join(k.value for k in allowed),
                code="unsupported_balance_kind",
            )
        return kind

    def upsert_threshold(
        self, slug: str, body: ThresholdUpsert, ctx: RequestContext
    ) -> ThresholdRead:
        provider = self.providers.require_provider(slug)
        balance_kind = self._validate(provider, body)
        values: dict[str, Any] = {
            "balance_kind": balance_kind,
            "amount": body.amount,
            "unit": body.unit,
            "unit_type": body.unit_type,
            "is_enabled": body.is_enabled,
            "notify_on_breach": body.notify_on_breach,
            "notify_on_recovery": body.notify_on_recovery,
            "recovery_margin": body.recovery_margin,
            "cooldown_minutes": body.cooldown_minutes,
            "notification_emails": body.notification_emails,
            "eligible_sources": list(body.eligible_sources),
            "monitoring_interval_minutes": body.monitoring_interval_minutes,
        }

        try:
            with transaction(self.session):
                threshold = self.thresholds.get_for_provider(provider.id)
                if threshold is None:
                    before = None
                    threshold = ThresholdConfig(
                        provider_id=provider.id, alert_state=AlertState.UNKNOWN, **values
                    )
                    self.thresholds.add(threshold)
                    action = "threshold.create"
                else:
                    before = _snapshot(threshold)
                    reset = any(getattr(threshold, f) != values[f] for f in _STATE_RESET_FIELDS)
                    for field, value in values.items():
                        setattr(threshold, field, value)
                    if reset and threshold.alert_state is not AlertState.UNKNOWN:
                        threshold.alert_state = AlertState.UNKNOWN
                        threshold.state_changed_at = utcnow()
                    self.session.flush()
                    action = "threshold.update"

                self.audit.record(
                    actor=ctx.actor,
                    action=action,
                    entity_type="threshold_config",
                    entity_id=threshold.id,
                    request_id=ctx.request_id,
                    client_ip=ctx.client_ip,
                    changes={
                        "provider": provider.slug,
                        "before": before,
                        "after": _snapshot(threshold),
                    },
                )
        except StaleDataError as exc:
            raise ConflictError(
                "Threshold was modified concurrently; reload and retry",
                code="concurrent_modification",
            ) from exc
        # Takes effect immediately: evaluate against the current balance (no restart).
        AlertEvaluator(self.session, self.settings).evaluate_provider(slug)
        self.session.refresh(threshold)
        return to_threshold_read(threshold, provider, self.settings)
