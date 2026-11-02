"""Outbox delivery: success, retries with bounded backoff, permanent failures, claim
exclusivity, crash leases, and the real SMTP protocol against a local sink."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import AlertDeliveryAttempt, AlertEvent
from app.models.enums import AlertEventType, AttemptOutcome, DeliveryStatus
from app.notifications.outbox import OutboxDispatcher, build_message
from app.notifications.smtp import DeliveryError, SmtpSender, classify_smtp_error
from tests.conftest import provider_by_slug
from tests.smtp_sink import SmtpSink

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeSender:
    configured = True

    def __init__(self, *failures: DeliveryError | None) -> None:
        self.failures = list(failures)
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        self.sent.append(message)


TEMP = DeliveryError("unavailable", "server down", retryable=True)
PERM = DeliveryError("recipients_refused", "refused", retryable=False)


@pytest.fixture
def mail_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "email_delivery_enabled": True,
            "smtp_host": "127.0.0.1",
            "smtp_from": "monitor@example.com",
            "alert_recipients": ["ops@example.com"],
            "alert_max_delivery_attempts": 3,
            "alert_retry_base_seconds": 60,
            "alert_retry_max_seconds": 3600,
        }
    )


def queue_event(session: Session, key: str = "t1:e1:breached", **kw) -> int:  # type: ignore[no-untyped-def]
    values = {
        "provider_id": provider_by_slug(session, "deepgram").id,
        "event_type": AlertEventType.BREACHED,
        "threshold_amount": 25,
        "unit": "USD",
        "recipients": ["ops@example.com"],
        "delivery_status": DeliveryStatus.PENDING,
        "dedupe_key": key,
        "subject": "[Low balance] Deepgram at 18.40 USD",
        "body_text": "plain body",
        "body_html": "<p>html body</p>",
        "next_attempt_at": T0,
        "occurred_at": T0,
    }
    values.update(kw)
    event = AlertEvent(**values)
    session.add(event)
    session.commit()
    return event.id


def dispatcher(app: FastAPI, settings: Settings, sender, clock: Clock) -> OutboxDispatcher:  # type: ignore[no-untyped-def]
    return OutboxDispatcher(
        app.state.session_factory, settings, sender, clock=clock, jitter=lambda: 1.0
    )


def reload(session: Session, event_id: int) -> AlertEvent:
    session.expire_all()
    event = session.get(AlertEvent, event_id)
    assert event is not None
    return event


def attempts(session: Session, event_id: int) -> list[AlertDeliveryAttempt]:
    return list(
        session.scalars(
            select(AlertDeliveryAttempt)
            .where(AlertDeliveryAttempt.alert_event_id == event_id)
            .order_by(AlertDeliveryAttempt.attempt_number)
        )
    )


def test_successful_delivery(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    event_id = queue_event(session)
    sender = FakeSender()
    [result] = dispatcher(app, mail_settings, sender, Clock()).run_once()
    assert result.status is DeliveryStatus.SENT
    event = reload(session, event_id)
    assert event.delivery_status is DeliveryStatus.SENT and event.sent_at == T0
    [msg] = sender.sent
    assert msg["To"] == "ops@example.com" and msg["From"] == "monitor@example.com"
    assert msg["Message-ID"] == f"<t1-e1-breached.{event_id}@api-cost-monitor.invalid>"
    assert msg.get_body(("html",)).get_content().strip() == "<p>html body</p>"  # type: ignore[union-attr]
    [attempt] = attempts(session, event_id)
    assert attempt.outcome is AttemptOutcome.SENT and attempt.attempt_number == 1
    assert dispatcher(app, mail_settings, sender, Clock()).run_once() == []  # nothing left


def test_transient_failure_retries_with_backoff(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    event_id = queue_event(session)
    sender = FakeSender(TEMP, TEMP)
    d = dispatcher(app, mail_settings, sender, clock)

    d.run_once()
    event = reload(session, event_id)
    assert event.delivery_status is DeliveryStatus.PENDING
    assert event.next_attempt_at == T0 + timedelta(seconds=60)
    assert "unavailable" in (event.last_error or "")

    assert d.run_once() == []  # not due yet: no hammering a dead SMTP server
    clock.now = T0 + timedelta(seconds=61)
    d.run_once()
    assert reload(session, event_id).next_attempt_at == clock.now + timedelta(seconds=120)

    clock.now += timedelta(seconds=121)
    d.run_once()
    assert reload(session, event_id).delivery_status is DeliveryStatus.SENT
    assert [a.outcome for a in attempts(session, event_id)] == [
        AttemptOutcome.RETRY,
        AttemptOutcome.RETRY,
        AttemptOutcome.SENT,
    ]


def test_gives_up_after_max_attempts_but_keeps_event(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    event_id = queue_event(session)
    d = dispatcher(app, mail_settings, FakeSender(TEMP, TEMP, TEMP, TEMP), clock)
    for _ in range(3):
        d.run_once()
        clock.now += timedelta(hours=2)
    event = reload(session, event_id)
    assert event.delivery_status is DeliveryStatus.FAILED and event.delivery_attempts == 3
    assert [a.outcome for a in attempts(session, event_id)][-1] is AttemptOutcome.FAILED


def test_permanent_failure_is_not_retried(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    event_id = queue_event(session)
    dispatcher(app, mail_settings, FakeSender(PERM), Clock()).run_once()
    event = reload(session, event_id)
    assert event.delivery_status is DeliveryStatus.FAILED and event.delivery_attempts == 1


def test_backoff_is_bounded(app, mail_settings) -> None:  # type: ignore[no-untyped-def]
    d = dispatcher(app, mail_settings, FakeSender(), Clock())
    assert [d.backoff(n).total_seconds() for n in (1, 2, 3, 7, 20)] == [60, 120, 240, 3600, 3600]


def test_claim_is_exclusive_and_lease_recovers_crashes(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    event_id = queue_event(session)
    a = dispatcher(app, mail_settings, FakeSender(), clock)
    b = dispatcher(app, mail_settings, FakeSender(), clock)
    assert a.claim(event_id) is True
    assert b.claim(event_id) is False  # second worker cannot take it
    # Worker A "crashes" while SENDING; after the lease expires the event is reclaimed.
    clock.now += timedelta(minutes=4)
    assert b.due_ids() == []
    clock.now += timedelta(minutes=2)
    assert b.due_ids() == [event_id]
    b.run_once()
    assert reload(session, event_id).delivery_status is DeliveryStatus.SENT


def test_delivery_disabled_after_creation_suppresses(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    event_id = queue_event(session)
    off = mail_settings.model_copy(update={"email_delivery_enabled": False})
    sender = FakeSender()
    dispatcher(app, off, sender, Clock()).run_once()
    assert reload(session, event_id).delivery_status is DeliveryStatus.SUPPRESSED
    assert sender.sent == []


def test_suppressed_and_sent_events_are_never_resent(app, session, mail_settings) -> None:  # type: ignore[no-untyped-def]
    queue_event(session, "a", delivery_status=DeliveryStatus.SUPPRESSED)
    queue_event(session, "b", delivery_status=DeliveryStatus.SENT)
    queue_event(session, "c", delivery_status=DeliveryStatus.FAILED)
    assert dispatcher(app, mail_settings, FakeSender(), Clock()).run_once() == []


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "auth_failed", True),
        (smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"no")}), "recipients_refused", False),
        (smtplib.SMTPResponseException(451, b"try later"), "temporary_failure", True),
        (smtplib.SMTPResponseException(554, b"rejected"), "rejected", False),
        (smtplib.SMTPServerDisconnected("gone"), "unavailable", True),
        (ConnectionRefusedError(), "unavailable", True),
        (TimeoutError(), "timeout", True),
    ],
)
def test_smtp_error_classification(exc: BaseException, code: str, retryable: bool) -> None:
    error = classify_smtp_error(exc)
    assert (error.code, error.retryable) == (code, retryable)


def sink_settings(settings: Settings, port: int, **kw) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_security": "none",
        "smtp_from": "monitor@example.com",
        "smtp_timeout_seconds": 5,
    }
    values.update(kw)
    return settings.model_copy(update=values)


def sample_message() -> EmailMessage:
    event = AlertEvent(
        id=7,
        event_type=AlertEventType.TEST,
        dedupe_key="test:1",
        subject="Hello",
        recipients=["ops@example.com"],
        body_text="text",
        body_html="<b>x</b>",
        occurred_at=T0,
        threshold_amount=0,
        unit="USD",
    )
    return build_message(event, "monitor@example.com")


def test_real_smtp_protocol_with_auth(settings: Settings) -> None:
    password = "sink-password-value-123456"
    with SmtpSink() as sink:
        sender = SmtpSender(
            sink_settings(
                settings, sink.port, smtp_username="mailer", smtp_password=SecretStr(password)
            )
        )
        sender.send(sample_message())
    [received] = sink.messages
    assert received.rcpt_to == ["ops@example.com"]
    assert received.message["Subject"] == "Hello"
    assert sink.state.auth_seen == [("mailer", password)]
    assert password.encode() not in received.raw  # credentials go to AUTH only, never the body


@pytest.mark.parametrize(
    ("mode", "code", "retryable"),
    [
        ("tempfail", "temporary_failure", True),
        ("reject_rcpt", "recipients_refused", False),
        ("auth_fail", "auth_failed", True),
    ],
)
def test_real_smtp_failures(settings: Settings, mode: str, code: str, retryable: bool) -> None:
    with SmtpSink() as sink:
        sink.state.mode = mode
        sender = SmtpSender(
            sink_settings(
                settings, sink.port, smtp_username="u", smtp_password=SecretStr("p-123456789")
            )
        )
        with pytest.raises(DeliveryError) as exc:
            sender.send(sample_message())
    assert (exc.value.code, exc.value.retryable) == (code, retryable)
    assert "p-123456789" not in str(exc.value)


def test_smtp_unreachable(settings: Settings) -> None:
    with SmtpSink() as sink:
        port = sink.port  # closed again after the block
    with pytest.raises(DeliveryError) as exc:
        SmtpSender(sink_settings(settings, port)).send(sample_message())
    assert exc.value.code == "unavailable" and exc.value.retryable


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (smtplib.SMTPSenderRefused(451, b"later", "a@b.c"), "temporary_failure", True),
        (smtplib.SMTPSenderRefused(553, b"no", "a@b.c"), "sender_refused", False),
        (smtplib.SMTPRecipientsRefused({"x@y.z": (450, b"busy")}), "temporary_failure", True),
        (
            smtplib.SMTPRecipientsRefused({"x@y.z": (450, b"busy"), "q@y.z": (550, b"no")}),
            "recipients_refused",
            False,
        ),
    ],
)
def test_reply_code_decides_retryability(exc: BaseException, code: str, retryable: bool) -> None:
    error = classify_smtp_error(exc)
    assert (error.code, error.retryable) == (code, retryable)


def test_starttls_required_but_unsupported_fails_safely(settings: Settings) -> None:
    with SmtpSink() as sink:  # the sink does not advertise STARTTLS
        sender = SmtpSender(sink_settings(settings, sink.port, smtp_security="starttls"))
        with pytest.raises(DeliveryError) as exc:
            sender.send(sample_message())
    assert exc.value.code == "tls_unavailable" and not exc.value.retryable
    assert sink.messages == []  # nothing sent in clear text
