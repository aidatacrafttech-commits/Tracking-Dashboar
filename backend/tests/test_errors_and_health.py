from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings


def test_health_payload(client: TestClient) -> None:
    data = client.get("/api/v1/health").json()
    assert data["status"] == "ok"
    assert data["time"].endswith("Z")


def test_ready_when_migrated(client: TestClient) -> None:
    resp = client.get("/api/v1/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok" and body["checks"]["migrations"] == "ok"
    assert body["checks"]["disk"] in ("ok", "warning")
    assert body["checks"]["scheduler"] == "disabled"  # tests run without the scheduler


def test_not_ready_when_migrations_pending(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path / "empty.db"))  # never migrated
    with TestClient(app) as c:
        resp = c.get("/api/v1/ready")
    app.state.engine.dispose()
    assert resp.status_code == 503
    assert resp.json()["checks"]["migrations"] == "pending_migrations"


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found"
    assert err["request_id"] == resp.headers["X-Request-ID"]


def test_method_not_allowed(client: TestClient, owner_headers: dict) -> None:
    resp = client.delete("/api/v1/providers/openai", headers=owner_headers)
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"


def test_request_id_propagation_and_sanitising(client: TestClient) -> None:
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert resp.headers["X-Request-ID"] != "bad id\nwith newline"
    assert len(resp.headers["X-Request-ID"]) == 32


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/api/v1/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_unhandled_exception_is_hidden(settings) -> None:  # type: ignore[no-untyped-def]
    app = create_app(settings)
    router = APIRouter()

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"secret detail {OWNER_TOKEN}")

    app.include_router(router)
    with TestClient(app) as c:
        resp = c.get("/boom", headers={"X-Request-ID": "req-500"})
    app.state.engine.dispose()
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "request_id": "req-500",
        }
    }
    assert resp.headers["X-Request-ID"] == "req-500"


def test_malformed_json_body(client: TestClient, owner_headers: dict) -> None:
    resp = client.post(
        "/api/v1/providers/openai/payments",
        content=b"{not json",
        headers={**owner_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_responses_never_contain_tokens(client: TestClient, owner_headers: dict) -> None:
    for path in ("/api/v1/providers", "/api/v1/settings", "/api/v1/health", "/api/v1/ready"):
        text = client.get(path, headers=owner_headers).text
        assert OWNER_TOKEN not in text and VIEWER_TOKEN not in text


def test_docs_disabled_in_production(tmp_path: Path, migrated_template: Path) -> None:
    db = tmp_path / "p.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(make_settings(db, app_env="production", cors_origins=[]))
    with TestClient(app) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
    app.state.engine.dispose()


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    ok = client.options(
        "/api/v1/providers",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options(
        "/api/v1/providers",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in bad.headers
