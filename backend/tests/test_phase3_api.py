"""End-to-end: mocked Deepgram balance -> poll -> threshold evaluation -> outbox -> real
SMTP protocol to a local sink. No real provider or mail server is contacted."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.models import AlertEvent
from app.providers.registry import build_runtimes
from tests.fakes import FakeHttp
from tests.smtp_sink import SmtpSink
from tests.test_polling import deepgram_fake, deepgram_runtime

DG_BALANCES = "https://api.deepgram.com/v1/projects/proj-1/balances"
SMTP_PASSWORD = "smtp-pass-must-never-leak-0123456789"


def set_balance(fake: FakeHttp, amount: float) -> None:
    fake.add(
        "GET",
        DG_BALANCES,
        httpx.Response(
            200, json={"balances": [{"balance_id": "b1", "amount": amount, "units": "USD"}]}
        ),
    )


@pytest.fixture
def sink() -> Iterator[SmtpSink]:
    with SmtpSink() as s:
        yield s


@pytest.fixture
def fake() -> FakeHttp:
    f = deepgram_fake()
    set_balance(f, 18.4)
    return f


def mail_settings(settings: Settings, sink: SmtpSink, **kw) -> Settings:  # type: ignore[no-untyped-def]
    values = {
        "email_delivery_enabled": True,
        "smtp_host": "127.0.0.1",
        "smtp_port": sink.port,
        "smtp_security": "none",
        "smtp_from": "monitor@example.com",
        "smtp_username": "mailer",
        "smtp_password": SecretStr(SMTP_PASSWORD),
        "alert_recipients": ["ops@example.com"],
        "dashboard_url": "https://monitor.example.com",
        "smtp_timeout_seconds": 5,
    }
    values.update(kw)
    return settings.model_copy(update=values)


def build(settings: Settings, fake: FakeHttp) -> FastAPI:
    from app.core.logging import register_secrets

    register_secrets(*settings.secret_values())
    runtimes = build_runtimes(settings)
    runtimes["deepgram"] = deepgram_runtime(fake)
    return create_app(settings, runtimes=runtimes, start_scheduler=False)


@pytest.fixture
def mail_app(settings: Settings, sink: SmtpSink, fake: FakeHttp) -> Iterator[FastAPI]:
    application = build(mail_settings(settings, sink), fake)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(mail_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(mail_app) as c:
        yield c


THRESHOLD = {"amount": "25.00", "unit": "USD", "recovery_margin": "5.00"}


def sync(api: TestClient, app: FastAPI, headers: dict) -> dict:
    app.state.coordinator._last_manual.clear()  # manual-sync cooldown is not under test here
    resp = api.post("/api/v1/providers/deepgram/sync", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_low_balance_email_end_to_end(api, mail_app, sink, fake, owner_headers) -> None:  # type: ignore[no-untyped-def]
    t = api.put(
        "/api/v1/providers/deepgram/threshold", json=THRESHOLD, headers=owner_headers
    ).json()
    assert t["last_evaluation_status"] == "no_data" and t["alert_state"] == "unknown"

    sync(api, mail_app, owner_headers)  # mocked balance 18.40 -> breach
    alerts = api.get("/api/v1/providers/deepgram/alerts", headers=owner_headers).json()
    assert [(a["event_type"], a["delivery_status"]) for a in alerts["items"]] == [
        ("breached", "pending")
    ]

    mail_app.state.dispatcher.run_once()  # what the scheduler does every tick
    [mail] = sink.messages
    msg = mail.message
    assert mail.rcpt_to == ["ops@example.com"]
    assert msg["Subject"] == "[Low balance] Deepgram at 18.40 USD (threshold 25.00 USD)"
    text = msg.get_body(("plain",)).get_content()  # type: ignore[union-attr]
    for expected in (
        "Current remaining balance: 18.40 USD",
        "Threshold: 25.00 USD",
        "Data source: Verified by the provider API",
        "Last successful sync:",
        "Last payment: 200.50 USD",
        "Recent usage (last 7 days)",
        "Recommended action: Top up the Deepgram project balance",
        "Dashboard: https://monitor.example.com",
    ):
        assert expected in text, expected
    assert SMTP_PASSWORD.encode() not in mail.raw
    assert b"dg-key-0123456789abcdef0123" not in mail.raw  # provider API key
    assert sink.state.auth_seen == [("mailer", SMTP_PASSWORD)]

    detail = api.get(f"/api/v1/alerts/{alerts['items'][0]['id']}", headers=owner_headers).json()
    assert detail["delivery_status"] == "sent"
    assert [a["outcome"] for a in detail["attempts"]] == ["sent"]

    # Repeated polling while still low: no new email.
    for _ in range(3):
        sync(api, mail_app, owner_headers)
        mail_app.state.dispatcher.run_once()
    assert len(sink.messages) == 1

    # Recovery above 25 + 5 -> one recovery email.
    set_balance(fake, 40)
    sync(api, mail_app, owner_headers)
    mail_app.state.dispatcher.run_once()
    assert len(sink.messages) == 2
    assert sink.messages[1].message["Subject"].startswith("[Recovered] Deepgram")
    t = api.get("/api/v1/providers/deepgram/threshold", headers=owner_headers).json()
    assert t["alert_state"] == "ok" and t["breach_episode"] == 1


def test_smtp_outage_keeps_alert_and_retries(api, mail_app, sink, owner_headers) -> None:  # type: ignore[no-untyped-def]
    api.put("/api/v1/providers/deepgram/threshold", json=THRESHOLD, headers=owner_headers)
    sink.state.mode = "tempfail"
    sync(api, mail_app, owner_headers)
    mail_app.state.dispatcher.run_once()
    [alert] = api.get("/api/v1/alerts", headers=owner_headers).json()["items"]
    assert alert["delivery_status"] == "pending" and "temporary_failure" in alert["last_error"]
    assert [a["outcome"] for a in alert["attempts"]] == ["retry"]

    sink.state.mode = "ok"
    later = utcnow() + timedelta(hours=1)
    mail_app.state.dispatcher.clock = lambda: later
    mail_app.state.dispatcher.run_once()
    alert = api.get(f"/api/v1/alerts/{alert['id']}", headers=owner_headers).json()
    assert alert["delivery_status"] == "sent"
    assert [a["outcome"] for a in alert["attempts"]] == ["retry", "sent"]
    assert len(sink.messages) == 1


def test_failed_alert_can_be_retried(api, mail_app, sink, owner_headers, viewer_headers) -> None:  # type: ignore[no-untyped-def]
    api.put("/api/v1/providers/deepgram/threshold", json=THRESHOLD, headers=owner_headers)
    sink.state.mode = "reject_rcpt"
    sync(api, mail_app, owner_headers)
    mail_app.state.dispatcher.run_once()
    [alert] = api.get("/api/v1/alerts", params={"status": "failed"}, headers=owner_headers).json()[
        "items"
    ]
    assert (
        api.post(f"/api/v1/alerts/{alert['id']}/retry", headers=viewer_headers).status_code == 403
    )
    sink.state.mode = "ok"
    retried = api.post(f"/api/v1/alerts/{alert['id']}/retry", headers=owner_headers).json()
    assert retried["delivery_status"] == "pending"
    mail_app.state.dispatcher.run_once()
    assert len(sink.messages) == 1
    detail = api.get(f"/api/v1/alerts/{alert['id']}", headers=owner_headers).json()
    assert [(a["attempt_number"], a["outcome"]) for a in detail["attempts"]] == [
        (1, "failed"),
        (2, "sent"),
    ]  # history kept across the manual retry
    again = api.post(f"/api/v1/alerts/{alert['id']}/retry", headers=owner_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "alert_not_failed"


def test_restart_does_not_duplicate_alerts(settings, sink, fake, owner_headers) -> None:  # type: ignore[no-untyped-def]
    cfg = mail_settings(settings, sink)
    first = build(cfg, fake)
    with TestClient(first) as c:
        c.put("/api/v1/providers/deepgram/threshold", json=THRESHOLD, headers=owner_headers)
        sync(c, first, owner_headers)
    first.state.engine.dispose()

    second = build(cfg, fake)  # "restarted" process, same database
    with TestClient(second) as c:
        sync(c, second, owner_headers)
        c.post("/api/v1/providers/deepgram/threshold/evaluate", headers=owner_headers)
        second.state.dispatcher.run_once()
        total = c.get("/api/v1/alerts", headers=owner_headers).json()["total"]
    second.state.engine.dispose()
    assert total == 1 and len(sink.messages) == 1


def test_delivery_disabled_records_but_never_sends(settings, sink, fake, owner_headers) -> None:  # type: ignore[no-untyped-def]
    app = build(mail_settings(settings, sink, email_delivery_enabled=False), fake)
    with TestClient(app) as c:
        c.put("/api/v1/providers/deepgram/threshold", json=THRESHOLD, headers=owner_headers)
        sync(c, app, owner_headers)
        app.state.dispatcher.run_once()
        [alert] = c.get("/api/v1/alerts", headers=owner_headers).json()["items"]
    app.state.engine.dispose()
    assert alert["delivery_status"] == "suppressed"
    assert "EMAIL_DELIVERY_ENABLED" in alert["last_error"]
    assert sink.messages == []


def test_preview_renders_without_side_effects(api, mail_app, owner_headers, viewer_headers) -> None:  # type: ignore[no-untyped-def]
    api.put(
        "/api/v1/providers/deepgram/threshold",
        json={**THRESHOLD, "amount": "10.00"},
        headers=owner_headers,
    )
    sync(api, mail_app, owner_headers)  # 18.40 > 10: no alert
    preview = api.get("/api/v1/providers/deepgram/alert-preview", headers=viewer_headers).json()
    assert preview["subject"] == "[Low balance] Deepgram at 18.40 USD (threshold 10.00 USD)"
    assert "<!doctype html>" in preview["html"] and "Nothing was sent" not in preview["html"]
    assert "Preview only" in preview["note"]
    session = mail_app.state.session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(AlertEvent)) == 0
    finally:
        session.close()
    recovered = api.get(
        "/api/v1/providers/deepgram/alert-preview",
        params={"event_type": "recovered"},
        headers=viewer_headers,
    ).json()
    assert recovered["subject"].startswith("[Recovered]")


def test_test_email_endpoint(api, sink, owner_headers, viewer_headers) -> None:  # type: ignore[no-untyped-def]
    assert (
        api.post("/api/v1/notifications/test-email", json={}, headers=viewer_headers).status_code
        == 403
    )
    assert api.post("/api/v1/notifications/test-email", json={}).status_code == 401
    resp = api.post(
        "/api/v1/notifications/test-email", json={"to": "Me@Example.com"}, headers=owner_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivery_status"] == "sent" and resp.json()["recipients"] == [
        "me@example.com"
    ]
    [mail] = sink.messages
    assert mail.message["Subject"] == "[Test] API Cost Monitor email delivery check"
    bad = api.post(
        "/api/v1/notifications/test-email", json={"to": "not-an-email"}, headers=owner_headers
    )
    assert bad.status_code == 422


def test_test_email_requires_smtp(settings, fake, owner_headers) -> None:  # type: ignore[no-untyped-def]
    app = build(settings, fake)  # no SMTP settings at all
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/notifications/test-email", json={"to": "a@example.com"}, headers=owner_headers
        )
    app.state.engine.dispose()
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "smtp_not_configured"


def test_notification_status_hides_credentials(api, viewer_headers) -> None:  # type: ignore[no-untyped-def]
    resp = api.get("/api/v1/notifications/status", headers=viewer_headers)
    body = resp.json()
    assert body["smtp_configured"] is True and body["smtp_auth_configured"] is True
    assert SMTP_PASSWORD not in resp.text and "mailer" not in resp.text
    assert set(body["outbox"]) == {"pending", "sending", "sent", "failed", "suppressed"}


def test_threshold_settings_editable_at_runtime(api, mail_app, owner_headers) -> None:  # type: ignore[no-untyped-def]
    body = {
        **THRESHOLD,
        "cooldown_minutes": 120,
        "monitoring_interval_minutes": 10,
        "eligible_sources": ["api_verified", "manual", "usage_estimate"],
        "notification_emails": ["oncall@example.com"],
    }
    t = api.put("/api/v1/providers/deepgram/threshold", json=body, headers=owner_headers).json()
    assert t["cooldown_minutes"] == 120 and t["monitoring_interval_minutes"] == 10
    assert t["effective_recipients"] == ["oncall@example.com"]
    assert t["recovery_boundary"] == "30"
    assert t["eligible_sources"] == ["api_verified", "manual", "usage_estimate"]
    import asyncio

    due = asyncio.run(mail_app.state.coordinator.due_operations("deepgram"))
    assert "balance" in [op.value for op in due]
    for bad in (
        {**THRESHOLD, "cooldown_minutes": 5},
        {**THRESHOLD, "eligible_sources": []},
        {**THRESHOLD, "eligible_sources": ["guess"]},
        {**THRESHOLD, "monitoring_interval_minutes": 1},
    ):
        assert (
            api.put(
                "/api/v1/providers/deepgram/threshold", json=bad, headers=owner_headers
            ).status_code
            == 422
        )


def test_manual_balance_entry_triggers_alert(api, mail_app, owner_headers) -> None:  # type: ignore[no-untyped-def]
    api.put(
        "/api/v1/providers/openai/threshold",
        json={"amount": "20.00", "unit": "USD"},
        headers=owner_headers,
    )
    resp = api.post(
        "/api/v1/providers/openai/balance-entries",
        json={"amount": "12.00", "unit": "USD"},
        headers=owner_headers,
    )
    assert resp.status_code == 201
    [alert] = api.get("/api/v1/providers/openai/alerts", headers=owner_headers).json()["items"]
    assert alert["event_type"] == "breached"
    detail = api.get(f"/api/v1/alerts/{alert['id']}", headers=owner_headers).json()
    assert detail["context"]["source_type"] == "manual"
    assert "Entered manually" in detail["body_text"]
