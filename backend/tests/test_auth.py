from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PAYMENT = {"amount": "10.00", "currency": "USD", "transaction_date": "2026-10-01T10:00:00+05:30"}
THRESHOLD = {"amount": "5.00", "unit": "USD"}

PROTECTED_READS = [
    "/api/v1/providers",
    "/api/v1/providers/openai",
    "/api/v1/providers/openai/snapshots",
    "/api/v1/providers/openai/payments",
    "/api/v1/providers/openai/threshold",
    "/api/v1/settings",
]


def test_health_and_ready_are_public(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/ready").status_code == 200


@pytest.mark.parametrize("path", PROTECTED_READS)
def test_reads_require_token(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token-0123456789-abcdefghijklmnopq",
        "Basic dXNlcjpwYXNz",
        "Bearer ",
        "test-owner-token-0123456789-abcdefghijklmnop",  # missing scheme
    ],
)
def test_invalid_credentials_rejected(client: TestClient, header: str) -> None:
    resp = client.get("/api/v1/providers", headers={"Authorization": header})
    assert resp.status_code == 401


def test_viewer_can_read_but_not_write(client: TestClient, viewer_headers: dict) -> None:
    assert client.get("/api/v1/providers", headers=viewer_headers).status_code == 200
    resp = client.post("/api/v1/providers/openai/payments", json=PAYMENT, headers=viewer_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    resp = client.put("/api/v1/providers/openai/threshold", json=THRESHOLD, headers=viewer_headers)
    assert resp.status_code == 403
    resp = client.put("/api/v1/settings", json={"display_timezone": "UTC"}, headers=viewer_headers)
    assert resp.status_code == 403


def test_owner_can_write(client: TestClient, owner_headers: dict) -> None:
    resp = client.post("/api/v1/providers/openai/payments", json=PAYMENT, headers=owner_headers)
    assert resp.status_code == 201
    resp = client.put("/api/v1/providers/openai/threshold", json=THRESHOLD, headers=owner_headers)
    assert resp.status_code == 200


def test_writes_without_token_rejected_before_validation(client: TestClient) -> None:
    # Unauthenticated callers must not learn anything about request validation.
    resp = client.post("/api/v1/providers/openai/payments", json={"bogus": 1})
    assert resp.status_code == 401


def test_viewer_disabled_when_not_configured(tmp_path, migrated_template) -> None:  # type: ignore[no-untyped-def]
    import shutil

    from app.main import create_app
    from tests.conftest import VIEWER_TOKEN, make_settings

    db = tmp_path / "v.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(make_settings(db, dashboard_viewer_token=None))
    with TestClient(app) as c:
        resp = c.get("/api/v1/providers", headers={"Authorization": f"Bearer {VIEWER_TOKEN}"})
    app.state.engine.dispose()
    assert resp.status_code == 401
