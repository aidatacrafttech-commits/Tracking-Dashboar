from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.time import utcnow
from app.db.session import transaction
from app.models import AlertDeliveryAttempt, AlertEvent, Provider, ThresholdConfig
from app.models.enums import AlertEventType, DeliveryStatus
from app.notifications.outbox import OutboxDispatcher
from app.notifications.smtp import SmtpSender
from app.notifications.templates import AlertEmailContext, render_email
from app.repositories.audit import AuditLogRepository
from app.schemas.alerts import (
    AlertEventDetail,
    AlertEventRead,
    AlertPage,
    DeliveryAttemptRead,
    EmailPreviewRead,
    EvaluationRead,
    NotificationStatusRead,
    TestEmailRead,
)
from app.services.alerts import AlertEvaluator, recipients_for
from app.services.context import RequestContext
from app.services.providers import ProviderService


class NotificationService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        dispatcher: OutboxDispatcher,
        sender: SmtpSender,
    ) -> None:
        self.session = session
        self.settings = settings
        self.dispatcher = dispatcher
        self.sender = sender
        self.providers = ProviderService(session)
        self.audit = AuditLogRepository(session)

    # --- reads ---------------------------------------------------------------------------
    def _read(self, event: AlertEvent, slug: str, detail: bool = False) -> AlertEventRead:
        attempts = list(
            self.session.scalars(
                select(AlertDeliveryAttempt)
                .where(AlertDeliveryAttempt.alert_event_id == event.id)
                .order_by(AlertDeliveryAttempt.attempt_number)
            )
        )
        data = {
            **{
                f: getattr(event, f)
                for f in (AlertEventDetail if detail else AlertEventRead).model_fields
                if f not in ("provider", "attempts", "source_type")
            },
            "provider": slug,
            "source_type": (event.context or {}).get("source_type"),
            "attempts": [DeliveryAttemptRead.model_validate(a) for a in attempts],
        }
        return (AlertEventDetail if detail else AlertEventRead).model_validate(data)

    def list_alerts(
        self,
        *,
        slug: str | None,
        status: DeliveryStatus | None,
        limit: int,
        offset: int,
    ) -> AlertPage:
        query = select(AlertEvent, Provider.slug).join(
            Provider, Provider.id == AlertEvent.provider_id
        )
        if slug is not None:
            query = query.where(AlertEvent.provider_id == self.providers.require_provider(slug).id)
        if status is not None:
            query = query.where(AlertEvent.delivery_status == status)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.execute(
            query.order_by(AlertEvent.occurred_at.desc(), AlertEvent.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return AlertPage(
            items=[self._read(e, s) for e, s in rows], total=total, limit=limit, offset=offset
        )

    def get_alert(self, event_id: int) -> AlertEventDetail:
        row = self.session.execute(
            select(AlertEvent, Provider.slug)
            .join(Provider, Provider.id == AlertEvent.provider_id)
            .where(AlertEvent.id == event_id)
        ).first()
        if row is None:
            raise NotFoundError(f"Alert {event_id} not found", code="alert_not_found")
        return self._read(row[0], row[1], detail=True)  # type: ignore[return-value]

    def status(self) -> NotificationStatusRead:
        counts = dict(
            self.session.execute(
                select(AlertEvent.delivery_status, func.count()).group_by(
                    AlertEvent.delivery_status
                )
            ).all()
        )
        s = self.settings
        return NotificationStatusRead(
            email_delivery_enabled=s.email_delivery_enabled,
            smtp_configured=self.sender.configured,
            smtp_host=s.smtp_host,
            smtp_port=s.smtp_port,
            smtp_security=s.smtp_security,
            smtp_from=s.smtp_from,
            smtp_auth_configured=bool(s.smtp_username and s.smtp_password),
            default_recipients=s.alert_recipients,
            dashboard_url=s.dashboard_url,
            exchange_rates_configured=bool(s.exchange_rates and s.exchange_rate_source),
            exchange_rate_source=s.exchange_rate_source,
            outbox={status.value: int(counts.get(status, 0)) for status in DeliveryStatus},
        )

    def audit_action(
        self,
        ctx: RequestContext,
        action: str,
        entity_type: str,
        entity_id: str | int | None,
        changes: dict | None = None,
    ) -> None:
        with transaction(self.session):
            self.audit.record(
                actor=ctx.actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes=changes,
            )

    # --- evaluation / preview --------------------------------------------------------------
    def evaluate(self, slug: str) -> EvaluationRead:
        self.providers.require_provider(slug)
        result = AlertEvaluator(self.session, self.settings).evaluate_provider(slug)
        if result is None:
            raise NotFoundError(
                f"No threshold configured for '{slug}'", code="threshold_not_configured"
            )
        return EvaluationRead(
            provider=result.provider,
            status=result.status,
            message=result.message,
            alert_state=result.alert_state,
            balance=result.balance,
            unit=result.unit,
            snapshot_id=result.snapshot_id,
            events_created=result.events_created,
            skipped_concurrent=result.skipped_concurrent,
        )

    def preview(self, slug: str, event_type: AlertEventType) -> EmailPreviewRead:
        """Render the email for the current data without creating or sending anything."""
        provider = self.providers.require_provider(slug)
        threshold = self.session.scalar(
            select(ThresholdConfig).where(ThresholdConfig.provider_id == provider.id)
        )
        if threshold is None:
            raise NotFoundError(
                f"No threshold configured for '{slug}'", code="threshold_not_configured"
            )
        evaluator = AlertEvaluator(self.session, self.settings)
        try:
            snapshot = evaluator._latest_eligible(provider, threshold)
            value = None
            note = "Preview only; nothing was sent or recorded."
            if snapshot is not None:
                value, conversion, problem = evaluator._comparable(snapshot, threshold)
                if problem is not None:
                    note += f" Current data would not trigger an alert: {problem[1]}."
            else:
                note += " No eligible balance yet; values show as unknown."
            context = evaluator.build_context(
                provider, threshold, snapshot, event_type, value, utcnow()
            )
            subject, text, html_body = render_email(context)
        finally:
            self.session.rollback()  # guarantee the preview wrote nothing
        recipients = recipients_for(threshold, self.settings)
        return EmailPreviewRead(
            provider=slug,
            event_type=event_type,
            subject=subject,
            text=text,
            html=html_body,
            recipients=recipients,
            email_delivery_enabled=self.settings.email_delivery_enabled,
            note=note,
        )

    # --- actions ---------------------------------------------------------------------------
    def retry(self, event_id: int, ctx: RequestContext) -> AlertEventDetail:
        with transaction(self.session):
            event = self.session.get(AlertEvent, event_id)
            if event is None:
                raise NotFoundError(f"Alert {event_id} not found", code="alert_not_found")
            if event.delivery_status is not DeliveryStatus.FAILED:
                raise ConflictError("Only failed alerts can be retried", code="alert_not_failed")
            event.delivery_status = DeliveryStatus.PENDING
            event.delivery_attempts = 0  # a fresh attempt budget; history stays in attempts
            event.next_attempt_at = utcnow()
            self.audit.record(
                actor=ctx.actor,
                action="alert.retry",
                entity_type="alert_event",
                entity_id=event.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
            )
        return self.get_alert(event_id)

    def send_test(self, to: str | None, ctx: RequestContext) -> TestEmailRead:
        if not self.sender.configured:
            raise DomainValidationError(
                "SMTP is not configured (SMTP_HOST, SMTP_FROM)", code="smtp_not_configured"
            )
        recipients = [to] if to else list(self.settings.alert_recipients)
        if not recipients:
            raise DomainValidationError(
                "No recipient given and ALERT_RECIPIENTS is empty", code="no_recipients"
            )
        any_provider = self.session.scalar(select(Provider).order_by(Provider.id).limit(1))
        if any_provider is None:  # providers are seeded by migration 0001
            raise DomainValidationError("No providers in the database", code="not_migrated")
        now = utcnow()
        context = AlertEmailContext(
            provider_slug="test",
            provider_name="Test",
            event_type="test",
            balance=None,
            threshold=0,
            unit="USD",
            recovery_boundary=0,
            source_type=None,  # type: ignore[arg-type]
            observed_at=None,
            last_sync_at=None,
            data_age_seconds=None,
            display_timezone="UTC",
            dashboard_url=self.settings.dashboard_url,
        )
        subject, text, html_body = render_email(context)
        with transaction(self.session):
            event = AlertEvent(
                provider_id=any_provider.id,
                event_type=AlertEventType.TEST,
                threshold_amount=0,
                unit="USD",
                recipients=recipients,
                delivery_status=DeliveryStatus.PENDING,
                dedupe_key=f"test:{uuid.uuid4().hex}",
                subject=subject,
                body_text=text,
                body_html=html_body,
                next_attempt_at=now,
                occurred_at=now,
            )
            self.session.add(event)
            self.session.flush()
            self.audit.record(
                actor=ctx.actor,
                action="notification.test_email",
                entity_type="alert_event",
                entity_id=event.id,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={"recipients": recipients},
            )
            event_id = event.id
        result = self.dispatcher.deliver(event_id)
        return TestEmailRead(
            event_id=event_id,
            recipients=recipients,
            delivery_status=result.status,
            error_code=result.error_code,
            error_message=result.error_message,
        )
