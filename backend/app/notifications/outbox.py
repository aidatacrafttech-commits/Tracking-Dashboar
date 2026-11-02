"""Outbox dispatcher: delivers pending alert_events by email.

* Claim: a conditional UPDATE moves an event to SENDING with a lease. Only one worker can
  win the claim (rowcount == 1), so two dispatchers never send the same event at once.
* Crash safety: an event stuck in SENDING past its lease is claimed again. Delivery is
  therefore at-least-once; every message carries a stable Message-ID derived from the
  event so mail clients can collapse an occasional duplicate.
* Retries: transient failures reschedule with bounded exponential backoff until
  ALERT_MAX_DELIVERY_ATTEMPTS, then the event becomes FAILED (it stays recorded).
* Every attempt is written to alert_delivery_attempts.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.time import utcnow
from app.db.session import transaction
from app.models import AlertDeliveryAttempt, AlertEvent
from app.models.enums import AlertEventType, AttemptOutcome, DeliveryStatus
from app.notifications.smtp import DeliveryError, SmtpSender

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=5)
BATCH = 20


@dataclass
class DeliveryResult:
    event_id: int
    outcome: AttemptOutcome | None  # None = not claimed (someone else has it / not due)
    status: DeliveryStatus | None = None
    error_code: str | None = None
    error_message: str | None = None


def message_id_for(event: AlertEvent) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]", "-", event.dedupe_key)
    return f"<{token}.{event.id}@api-cost-monitor.invalid>"


def build_message(event: AlertEvent, sender: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = event.subject or "API Cost Monitor alert"
    message["From"] = sender
    message["To"] = ", ".join(event.recipients or [])
    message["Date"] = format_datetime(event.occurred_at)
    message["Message-ID"] = message_id_for(event) if event.id else make_msgid()
    message["X-Alert-Type"] = event.event_type.value
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(event.body_text or "")
    if event.body_html:
        message.add_alternative(event.body_html, subtype="html")
    return message


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        sender: SmtpSender,
        clock: Callable[[], datetime] = utcnow,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),  # noqa: S311
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.sender = sender
        self.clock = clock
        self.jitter = jitter

    def backoff(self, attempt: int) -> timedelta:
        base = self.settings.alert_retry_base_seconds * (2 ** max(0, attempt - 1))
        return timedelta(seconds=min(base, self.settings.alert_retry_max_seconds) * self.jitter())

    def _due_condition(self, now: datetime):  # type: ignore[no-untyped-def]
        return or_(
            and_(
                AlertEvent.delivery_status == DeliveryStatus.PENDING,
                or_(AlertEvent.next_attempt_at.is_(None), AlertEvent.next_attempt_at <= now),
            ),
            and_(
                AlertEvent.delivery_status == DeliveryStatus.SENDING,
                AlertEvent.lease_expires_at < now,
            ),
        )

    def claim(self, event_id: int) -> bool:
        session = self.session_factory()
        try:
            with transaction(session):
                now = self.clock()
                result = session.execute(
                    update(AlertEvent)
                    .where(AlertEvent.id == event_id, self._due_condition(now))
                    .values(
                        delivery_status=DeliveryStatus.SENDING,
                        lease_expires_at=now + LEASE,
                        delivery_attempts=AlertEvent.delivery_attempts + 1,
                    )
                )
                return result.rowcount == 1
        finally:
            session.close()

    def due_ids(self, limit: int = BATCH) -> list[int]:
        session = self.session_factory()
        try:
            return list(
                session.scalars(
                    select(AlertEvent.id)
                    .where(self._due_condition(self.clock()))
                    .order_by(AlertEvent.occurred_at, AlertEvent.id)
                    .limit(limit)
                )
            )
        finally:
            session.close()

    def deliver(self, event_id: int) -> DeliveryResult:
        if not self.claim(event_id):
            return DeliveryResult(event_id, None)
        session = self.session_factory()
        try:
            event = session.get(AlertEvent, event_id)
            if event is None:  # deleted between claim and load
                return DeliveryResult(event_id, None)
            started = self.clock()
            attempt = event.delivery_attempts
            error: DeliveryError | None = None

            if (
                event.event_type is not AlertEventType.TEST
                and not self.settings.email_delivery_enabled
            ):
                return self._finish(
                    session,
                    event,
                    attempt,
                    started,
                    DeliveryStatus.SUPPRESSED,
                    None,
                    "email delivery disabled (EMAIL_DELIVERY_ENABLED=false)",
                )
            if not event.recipients:
                error = DeliveryError("no_recipients", "no recipients configured", retryable=False)
            else:
                try:
                    self.sender.send(build_message(event, self.settings.smtp_from or ""))
                except DeliveryError as exc:
                    error = exc
                except Exception as exc:  # defensive: never lose the event on a bug
                    logger.exception("unexpected delivery error")
                    error = DeliveryError(
                        "unexpected", f"unexpected error ({type(exc).__name__})", retryable=True
                    )

            if error is None:
                return self._finish(
                    session, event, attempt, started, DeliveryStatus.SENT, None, None
                )
            exhausted = attempt >= self.settings.alert_max_delivery_attempts
            if error.retryable and not exhausted:
                return self._finish(
                    session,
                    event,
                    attempt,
                    started,
                    DeliveryStatus.PENDING,
                    error,
                    None,
                    retry_at=self.clock() + self.backoff(attempt),
                )
            return self._finish(
                session, event, attempt, started, DeliveryStatus.FAILED, error, None
            )
        finally:
            session.close()

    def _finish(
        self,
        session: Session,
        event: AlertEvent,
        attempt: int,
        started: datetime,
        status: DeliveryStatus,
        error: DeliveryError | None,
        note: str | None,
        retry_at: datetime | None = None,
    ) -> DeliveryResult:
        finished = self.clock()
        if status is DeliveryStatus.SENT:
            outcome = AttemptOutcome.SENT
        elif status is DeliveryStatus.PENDING:
            outcome = AttemptOutcome.RETRY
        else:
            outcome = AttemptOutcome.FAILED
        with transaction(session):
            event.delivery_status = status
            event.lease_expires_at = None
            event.next_attempt_at = retry_at
            if status is DeliveryStatus.SENT:
                event.sent_at = finished
                event.last_error = None
            else:
                event.last_error = (f"{error.code}: {error.message}" if error else note or "")[:500]
            if status is not DeliveryStatus.SUPPRESSED:
                # History numbering continues across manual retries (which reset the
                # delivery_attempts budget), so attempt numbers stay unique per event.
                previous = session.scalar(
                    select(func.max(AlertDeliveryAttempt.attempt_number)).where(
                        AlertDeliveryAttempt.alert_event_id == event.id
                    )
                )
                session.add(
                    AlertDeliveryAttempt(
                        alert_event_id=event.id,
                        attempt_number=(previous or 0) + 1,
                        outcome=outcome,
                        error_code=error.code if error else None,
                        error_message=error.message[:500] if error else None,
                        smtp_status=error.smtp_status if error else None,
                        started_at=started,
                        finished_at=finished,
                    )
                )
        log = logger.info if status is DeliveryStatus.SENT else logger.warning
        log(
            "alert delivery",
            extra={
                "event_id": event.id,
                "status": status.value,
                "attempt": attempt,
                "error_code": error.code if error else None,
            },
        )
        return DeliveryResult(
            event.id,
            outcome,
            status,
            error.code if error else None,
            error.message if error else note,
        )

    def run_once(self) -> list[DeliveryResult]:
        return [self.deliver(event_id) for event_id in self.due_ids()]
