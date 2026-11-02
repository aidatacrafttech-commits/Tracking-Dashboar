from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import AuditLog, PaymentTransaction

URL = "/api/v1/providers/openai/payments"


def body(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "amount": "50.00",
        "currency": "USD",
        "transaction_date": "2026-10-01T10:00:00+05:30",
        "kind": "top_up",
        "payment_reference": "INV-001",
        "notes": "Initial top-up",
    }
    data.update(overrides)
    return data


def error_code(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def test_create_payment_returns_utc_and_decimal_string(
    client: TestClient, owner_headers: dict, session: Session
) -> None:
    resp = client.post(URL, json=body(), headers=owner_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["amount"] == "50"
    assert data["currency"] == "USD"
    assert data["transaction_date"] == "2026-10-01T04:30:00Z"
    assert data["entry_method"] == "manual"
    assert data["provider"] == "openai"

    audit = session.scalars(select(AuditLog).where(AuditLog.action == "payment.create")).one()
    assert audit.actor.value == "owner"
    assert audit.entity_id == str(data["id"])
    assert audit.request_id == resp.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"amount": "0"}, "amount"),
        ({"amount": "-1.00"}, "amount"),
        ({"amount": "10.001"}, "amount"),  # USD has 2 minor units
        ({"amount": "abc"}, "amount"),
        ({"amount": 10.5}, "amount"),  # JSON float: precision risk
        ({"amount": "1e13"}, "amount"),
        ({"currency": "US"}, "currency"),
        ({"currency": "U$D"}, "currency"),
        ({"transaction_date": "2026-10-01T10:00:00"}, "transaction_date"),  # naive
        ({"kind": "gift"}, "kind"),
        ({"payment_reference": "x" * 129}, "payment_reference"),
        ({"notes": "x" * 2001}, "notes"),
        ({"entry_method": "provider_verified"}, "entry_method"),  # not client-settable
    ],
)
def test_payment_validation(
    client: TestClient, owner_headers: dict, overrides: dict, field: str
) -> None:
    resp = client.post(URL, json=body(**overrides), headers=owner_headers)
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "validation_error"
    assert any(field in d["loc"] for d in err["details"])
    assert all("input" not in d for d in err["details"])  # submitted values not echoed


def test_unsupported_currency(client: TestClient, owner_headers: dict) -> None:
    resp = client.post(URL, json=body(currency="JPY"), headers=owner_headers)
    assert resp.status_code == 422
    assert error_code(resp) == "unsupported_currency"
    assert "USD" in resp.json()["error"]["details"]["supported"]


def test_future_transaction_date_rejected(client: TestClient, owner_headers: dict) -> None:
    future = (utcnow() + timedelta(days=1)).isoformat()
    resp = client.post(URL, json=body(transaction_date=future), headers=owner_headers)
    assert resp.status_code == 422
    assert error_code(resp) == "future_transaction_date"


def test_currency_is_normalized(client: TestClient, owner_headers: dict) -> None:
    resp = client.post(URL, json=body(currency="inr", amount="1500.5"), headers=owner_headers)
    assert resp.status_code == 201
    assert resp.json()["currency"] == "INR"
    assert resp.json()["amount"] == "1500.5"


def test_duplicate_reference_conflict(client: TestClient, owner_headers: dict) -> None:
    assert client.post(URL, json=body(), headers=owner_headers).status_code == 201
    resp = client.post(URL, json=body(), headers=owner_headers)
    assert resp.status_code == 409
    assert error_code(resp) == "duplicate_payment_reference"
    # Same reference on a different provider is allowed.
    other = client.post(
        "/api/v1/providers/sarvam/payments", json=body(currency="INR"), headers=owner_headers
    )
    assert other.status_code == 201


def test_unknown_provider(client: TestClient, owner_headers: dict) -> None:
    resp = client.post("/api/v1/providers/nope/payments", json=body(), headers=owner_headers)
    assert resp.status_code == 404
    assert error_code(resp) == "provider_not_found"


def test_invalid_provider_slug_format(client: TestClient, owner_headers: dict) -> None:
    resp = client.get("/api/v1/providers/Bad%20Slug!/payments", headers=owner_headers)
    assert resp.status_code == 422


def test_list_payments_with_totals_and_range(
    client: TestClient, owner_headers: dict, viewer_headers: dict
) -> None:
    for i, (amount, kind, date) in enumerate(
        [
            ("100.00", "top_up", "2026-09-01T00:00:00Z"),
            ("25.50", "top_up", "2026-10-02T00:00:00Z"),
            ("10.25", "refund", "2026-10-03T00:00:00Z"),
        ]
    ):
        resp = client.post(
            URL,
            json=body(amount=amount, kind=kind, transaction_date=date, payment_reference=f"R{i}"),
            headers=owner_headers,
        )
        assert resp.status_code == 201

    resp = client.get(URL, headers=viewer_headers)
    data = resp.json()
    assert data["total"] == 3
    assert [p["amount"] for p in data["items"]] == ["10.25", "25.5", "100"]  # newest first
    assert data["totals"] == [{"currency": "USD", "net_amount": "115.25", "count": 3}]

    resp = client.get(
        URL, params={"start": "2026-10-01T00:00:00Z", "limit": 1}, headers=viewer_headers
    )
    data = resp.json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert data["totals"] == [{"currency": "USD", "net_amount": "15.25", "count": 2}]


def test_list_payments_rejects_bad_range_and_paging(
    client: TestClient, viewer_headers: dict
) -> None:
    resp = client.get(
        URL,
        params={"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
        headers=viewer_headers,
    )
    assert resp.status_code == 422 and error_code(resp) == "invalid_range"
    assert client.get(URL, params={"limit": 0}, headers=viewer_headers).status_code == 422
    assert client.get(URL, params={"limit": 501}, headers=viewer_headers).status_code == 422
    resp = client.get(URL, params={"start": "2026-10-01T00:00:00"}, headers=viewer_headers)
    assert resp.status_code == 422  # naive datetime


def test_payment_and_audit_are_atomic(
    client: TestClient, owner_headers: dict, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.audit import AuditLogRepository

    def boom(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(AuditLogRepository, "record", boom)
    resp = client.post(URL, json=body(), headers=owner_headers)
    assert resp.status_code == 500
    assert error_code(resp) == "internal_error"
    assert "audit store" not in resp.text
    count = session.scalar(select(func.count()).select_from(PaymentTransaction))
    assert count == 0
