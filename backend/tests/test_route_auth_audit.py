"""Every API operation is enumerated from the app itself, so a route added later without
authentication, role or CSRF protection fails this test (production-readiness audit)."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN

PUBLIC = {"/api/v1/health", "/api/v1/ready", "/api/v1/auth/login"}
# Answer anonymously but only about the caller's own session (401 without one).
SESSION_ONLY = {"/api/v1/auth/session", "/api/v1/auth/logout"}
ORIGIN = {"Origin": "http://testserver"}
# Not in the OpenAPI schema (streaming), checked explicitly.
EXTRA_PRIVATE = [("GET", "/api/v1/events")]


def operations(app: FastAPI) -> list[tuple[str, str]]:
    spec = app.openapi()
    ops = [(m.upper(), p) for p, item in spec["paths"].items() for m in item]
    return ops + [op for op in EXTRA_PRIVATE if op not in ops]


def concrete(path: str) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: "deepgram" if "provider" in m.group(1) else "1", path)


# Integration endpoints accept only VOUCHER_API_TOKEN (never sessions or dashboard tokens),
# so dashboard credentials get 401 there; they are checked separately below.
INTEGRATION = "/api/v1/integrations/"


def writes(ops: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (m, p)
        for m, p in ops
        if m != "GET" and not p.startswith("/api/v1/auth/") and not p.startswith(INTEGRATION)
    ]


def test_route_inventory_is_complete(app: FastAPI) -> None:
    ops = operations(app)
    assert len(ops) >= 39 and ("GET", "/api/v1/events") in ops
    assert all(p.startswith("/api/v1/") for _, p in ops)


def test_every_private_operation_requires_authentication(client: TestClient, app: FastAPI) -> None:
    open_routes = []
    for method, path in operations(app):
        if path in PUBLIC:
            continue
        status = client.request(
            method, concrete(path), json={} if method != "GET" else None
        ).status_code
        if status != 401:
            open_routes.append((method, path, status))
    assert open_routes == []


def test_viewer_cannot_perform_any_write(client: TestClient, app: FastAPI) -> None:
    login = client.post("/api/v1/auth/login", json={"token": VIEWER_TOKEN}, headers=ORIGIN)
    csrf = login.json()["csrf_token"]
    allowed = [
        (m, p, s)
        for m, p in writes(operations(app))
        if (
            s := client.request(
                m, concrete(p), json={}, headers={**ORIGIN, "X-CSRF-Token": csrf}
            ).status_code
        )
        != 403
    ]
    assert allowed == []
    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
    assert [
        (m, p)
        for m, p in writes(operations(app))
        if client.request(m, concrete(p), json={}, headers=bearer).status_code != 403
    ] == []


def test_viewer_cannot_read_admin_operations(client: TestClient, app: FastAPI) -> None:
    client.post("/api/v1/auth/login", json={"token": VIEWER_TOKEN}, headers=ORIGIN)
    admin = [(m, p) for m, p in operations(app) if p.startswith("/api/v1/admin")]
    assert admin
    assert [
        (m, p) for m, p in admin if client.request(m, concrete(p), json={}).status_code != 403
    ] == []


def test_owner_cookie_writes_need_csrf_token(client: TestClient, app: FastAPI) -> None:
    client.post("/api/v1/auth/login", json={"token": OWNER_TOKEN}, headers=ORIGIN)
    unprotected = [
        (m, p)
        for m, p in writes(operations(app))
        if client.request(m, concrete(p), json={}, headers=ORIGIN).status_code != 403
    ]
    assert unprotected == []


def test_session_routes_reveal_nothing_without_a_session(client: TestClient) -> None:
    for path in SESSION_ONLY:
        method = "GET" if path.endswith("session") else "POST"
        resp = client.request(method, path, headers=ORIGIN)
        assert resp.status_code == 401 and "csrf_token" not in resp.text


def test_integration_endpoints_refuse_every_dashboard_credential(
    client: TestClient, app: FastAPI
) -> None:
    integration = [(m, p) for m, p in operations(app) if p.startswith(INTEGRATION)]
    assert integration  # register, redeem, capacity
    for headers in (
        {},
        {"Authorization": f"Bearer {OWNER_TOKEN}"},
        {"Authorization": f"Bearer {VIEWER_TOKEN}"},
    ):
        assert [
            (m, p, s)
            for m, p in integration
            if (s := client.request(m, concrete(p), json={}, headers=headers).status_code) != 401
        ] == []
    client.post("/api/v1/auth/login", json={"token": OWNER_TOKEN}, headers=ORIGIN)
    assert all(
        client.request(m, concrete(p), json={}, headers=ORIGIN).status_code == 401
        for m, p in integration
    )
