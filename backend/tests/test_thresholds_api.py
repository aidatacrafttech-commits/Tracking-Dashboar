from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, ThresholdConfig
from app.models.enums import AlertState


def url(slug: str = "deepgram") -> str:
    return f"/api/v1/providers/{slug}/threshold"


def code(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def test_get_unconfigured_threshold(client: TestClient, viewer_headers: dict) -> None:
    resp = client.get(url(), headers=viewer_headers)
    assert resp.status_code == 404
    assert code(resp) == "threshold_not_configured"


def test_create_and_read_threshold(client: TestClient, owner_headers: dict) -> None:
    payload = {
        "amount": "25.00",
        "unit": "usd",
        "recovery_margin": "5",
        "cooldown_minutes": 360,
        "notification_emails": ["Ops@Example.com", "ops@example.com"],
    }
    resp = client.put(url(), json=payload, headers=owner_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["amount"] == "25"
    assert data["unit"] == "USD"
    assert data["balance_kind"] == "prepaid_balance"
    assert data["alert_state"] == "unknown"
    assert data["notification_emails"] == ["ops@example.com"]
    # Saved thresholds are evaluated immediately; with no balance yet the state stays unknown.
    assert data["last_evaluation_status"] == "no_data"

    assert client.get(url(), headers=owner_headers).json() == data


def test_update_resets_alert_state_and_audits(
    client: TestClient, owner_headers: dict, session: Session
) -> None:
    resp = client.put(url(), json={"amount": "25", "unit": "USD"}, headers=owner_headers)
    assert resp.status_code == 200
    threshold = session.scalars(select(ThresholdConfig)).one()
    threshold.alert_state = AlertState.BREACHED
    session.commit()

    # Changing only notification prefs keeps the state.
    resp = client.put(
        url(),
        json={"amount": "25", "unit": "USD", "notify_on_recovery": False},
        headers=owner_headers,
    )
    assert resp.json()["alert_state"] == "breached"

    # Changing the amount invalidates it until the next evaluation.
    resp = client.put(url(), json={"amount": "40", "unit": "USD"}, headers=owner_headers)
    assert resp.json()["alert_state"] == "unknown"
    assert resp.json()["state_changed_at"] is not None

    actions = [a.action for a in session.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert actions == ["threshold.create", "threshold.update", "threshold.update"]
    last = session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert last is not None and last.changes is not None
    assert last.changes["before"]["amount"] == "25"
    assert last.changes["after"]["amount"] == "40"


def test_provider_specific_units_and_balance_kinds(client: TestClient, owner_headers: dict) -> None:
    resp = client.put(
        url("elevenlabs"), json={"amount": "10000", "unit": "CREDITS"}, headers=owner_headers
    )
    assert resp.status_code == 200
    assert resp.json()["unit_type"] == "credits"
    assert resp.json()["balance_kind"] == "plan_quota"

    resp = client.put(url("aws"), json={"amount": "50", "unit": "USD"}, headers=owner_headers)
    assert resp.json()["balance_kind"] == "promotional_credit"

    resp = client.put(url("sarvam"), json={"amount": "500", "unit": "INR"}, headers=owner_headers)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    ("slug", "payload", "expected_code"),
    [
        ("deepgram", {"amount": "10", "unit": "INR"}, "unit_mismatch"),
        ("elevenlabs", {"amount": "10", "unit": "USD"}, "unit_mismatch"),
        ("deepgram", {"amount": "10.001", "unit": "USD"}, "invalid_precision"),
        (
            "deepgram",
            {"amount": "10", "unit": "USD", "recovery_margin": "0.001"},
            "invalid_precision",
        ),
        (
            "deepgram",
            {"amount": "10", "unit": "USD", "balance_kind": "plan_quota"},
            "unsupported_balance_kind",
        ),
    ],
)
def test_domain_validation(
    client: TestClient, owner_headers: dict, slug: str, payload: dict, expected_code: str
) -> None:
    resp = client.put(url(slug), json=payload, headers=owner_headers)
    assert resp.status_code == 422
    assert code(resp) == expected_code


@pytest.mark.parametrize(
    "payload",
    [
        {"amount": "-1", "unit": "USD"},
        {"amount": 10.5, "unit": "USD"},
        {"amount": "10", "unit": "USD", "cooldown_minutes": 5},
        {"amount": "10", "unit": "USD", "notification_emails": ["not-an-email"]},
        {"amount": "10", "unit": "USD", "notification_emails": []},
        {"amount": "10", "unit": "USD", "unexpected": True},
        {"unit": "USD"},
    ],
)
def test_schema_validation(client: TestClient, owner_headers: dict, payload: dict) -> None:
    resp = client.put(url(), json=payload, headers=owner_headers)
    assert resp.status_code == 422
    assert code(resp) == "validation_error"


def test_unsupported_currency_when_not_in_allowlist(
    tmp_path,
    migrated_template,
    owner_headers: dict,  # type: ignore[no-untyped-def]
) -> None:
    import shutil

    from app.main import create_app
    from tests.conftest import make_settings

    db = tmp_path / "c.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(make_settings(db, supported_currencies=["INR"]))
    with TestClient(app) as c:
        resp = c.put(url(), json={"amount": "10", "unit": "USD"}, headers=owner_headers)
    app.state.engine.dispose()
    assert resp.status_code == 422
    assert code(resp) == "unsupported_currency"
