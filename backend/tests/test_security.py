"""Phase 5 security tests: sessions, CSRF/Origin, rate limits, injection, malformed input,
secret leakage, host/size limits, admin controls, locks, backups and readiness."""

from __future__ import annotations

import asyncio
import logging
import shutil
import smtplib
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError

from app.core.time import utcnow
from app.main import create_app
from app.models import AuditLog, AuthSession, MonitoringRun
from app.models.enums import RunStatus
from app.ops.locks import JobLocks
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings

ORIGIN = {"Origin": "http://testserver"}
PAYMENT = {"amount": "10.00", "currency": "USD", "transaction_date": "2026-10-01T10:00:00Z"}


def login(client: TestClient, token: str = OWNER_TOKEN, **extra_headers: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/auth/login", json={"token": token}, headers={**ORIGIN, **extra_headers}
    )


def session_client(client: TestClient, token: str = OWNER_TOKEN) -> dict[str, str]:
    resp = login(client, token)
    assert resp.status_code == 200, resp.text
    return {**ORIGIN, "X-CSRF-Token": resp.json()["csrf_token"]}


def audit_actions(app: FastAPI) -> list[str]:
    s = app.state.session_factory()
    try:
        return list(s.scalars(select(AuditLog.action).order_by(AuditLog.id)))
    finally:
        s.close()


# --- sessions --------------------------------------------------------------------------------


def test_login_sets_hardened_cookie_and_never_echoes_token(
    client: TestClient, app: FastAPI
) -> None:
    resp = login(client)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    for attr in ("HttpOnly", "SameSite=strict", "Path=/"):
        assert attr.lower() in cookie.lower(), attr
    assert OWNER_TOKEN not in resp.text and OWNER_TOKEN not in cookie
    body = resp.json()
    assert body["role"] == "owner" and body["via"] == "session" and len(body["csrf_token"]) == 64
    # the cookie alone authenticates reads; only a hash of it is stored
    assert client.get("/api/v1/providers").status_code == 200
    raw = client.cookies.get("acm_session")
    s = app.state.session_factory()
    try:
        stored = s.scalars(select(AuthSession.id_hash)).all()
    finally:
        s.close()
    assert raw not in stored and len(stored) == 1
    assert audit_actions(app)[-1] == "auth.login"


def test_secure_cookie_in_production(tmp_path: Path, migrated_template: Path) -> None:
    db = tmp_path / "p.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(
        make_settings(db, app_env="production", cors_origins=[]), start_scheduler=False
    )
    with TestClient(app, base_url="https://testserver") as c:
        resp = c.post(
            "/api/v1/auth/login",
            json={"token": OWNER_TOKEN},
            headers={"Origin": "https://testserver"},
        )
    app.state.engine.dispose()
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("__Host-acm_session=") and "secure" in cookie.lower()


def test_wrong_token_rejected_and_audited(client: TestClient, app: FastAPI) -> None:
    resp = login(client, "not-the-token")
    assert resp.status_code == 401 and "set-cookie" not in resp.headers
    assert audit_actions(app)[-1] == "auth.login_failed"


def test_cookie_writes_require_csrf_and_same_origin(client: TestClient) -> None:
    headers = session_client(client)
    url = "/api/v1/providers/openai/payments"
    no_csrf = client.post(url, json=PAYMENT, headers=ORIGIN)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
    bad_csrf = client.post(url, json=PAYMENT, headers={**ORIGIN, "X-CSRF-Token": "0" * 64})
    assert bad_csrf.json()["error"]["code"] == "csrf_failed"
    no_origin = client.post(url, json=PAYMENT, headers={"X-CSRF-Token": headers["X-CSRF-Token"]})
    assert no_origin.json()["error"]["code"] == "origin_required"
    evil = client.post(url, json=PAYMENT, headers={**headers, "Origin": "https://evil.example"})
    assert evil.json()["error"]["code"] == "origin_not_allowed"
    cross = client.post(url, json=PAYMENT, headers={**headers, "Sec-Fetch-Site": "cross-site"})
    assert cross.json()["error"]["code"] == "cross_site_request"
    assert client.post(url, json=PAYMENT, headers=headers).status_code == 201


def test_login_itself_requires_same_origin(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"token": OWNER_TOKEN},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_viewer_session_cannot_write(client: TestClient) -> None:
    headers = session_client(client, VIEWER_TOKEN)
    resp = client.post("/api/v1/providers/openai/payments", json=PAYMENT, headers=headers)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"


def test_logout_revokes_session(client: TestClient, app: FastAPI) -> None:
    headers = session_client(client)
    resp = client.post("/api/v1/auth/logout", headers=headers)
    assert resp.status_code == 204
    assert (
        'acm_session=""' in resp.headers["set-cookie"] or "Max-Age=0" in resp.headers["set-cookie"]
    )
    # Re-presenting the old cookie value does not work either.
    s = app.state.session_factory()
    try:
        assert s.scalars(select(AuthSession.revoked_at)).one() is not None
    finally:
        s.close()
    assert "auth.logout" in audit_actions(app)


@pytest.mark.parametrize("field", ["idle", "absolute"])
def test_sessions_expire(client: TestClient, app: FastAPI, field: str) -> None:
    session_client(client)
    s = app.state.session_factory()
    try:
        values = (
            {"last_seen_at": utcnow() - timedelta(hours=2)}
            if field == "idle"
            else {"expires_at": utcnow() - timedelta(seconds=1)}
        )
        s.execute(update(AuthSession).values(**values))
        s.commit()
    finally:
        s.close()
    resp = client.get("/api/v1/providers")
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "session_expired"


def test_rotating_the_token_ends_its_sessions(db_path: Path) -> None:
    app = create_app(make_settings(db_path), start_scheduler=False)
    with TestClient(app) as c:
        session_client(c)
        cookie = c.cookies.get("acm_session")
    app.state.engine.dispose()
    rotated = create_app(
        make_settings(db_path, dashboard_owner_token="rotated-owner-token-0123456789-abcdefghij"),
        start_scheduler=False,
    )
    with TestClient(rotated) as c:
        c.cookies.set("acm_session", cookie)
        assert c.get("/api/v1/providers").status_code == 401
    rotated.state.engine.dispose()


def test_revoke_all_sessions(client: TestClient, app: FastAPI) -> None:
    headers = session_client(client)
    resp = client.post("/api/v1/admin/sessions/revoke-all", headers=headers)
    assert resp.status_code == 200 and resp.json()["revoked"] == 1
    assert client.get("/api/v1/providers").status_code == 401
    assert "auth.revoke_all" in audit_actions(app)


# --- rate limiting -------------------------------------------------------------------------------


def test_login_rate_limit(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "wrong-token").status_code == 401
    blocked = login(client, OWNER_TOKEN)  # even the right token is refused while limited
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"
    assert int(blocked.headers["Retry-After"]) > 0


def test_bearer_brute_force_is_limited(client: TestClient) -> None:
    for _ in range(20):
        assert (
            client.get(
                "/api/v1/providers",
                headers={"Authorization": "Bearer wrong-xxxxxxxxxxxxxxxxxxxxxxxx"},
            ).status_code
            == 401
        )
    assert (
        client.get(
            "/api/v1/providers", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
        ).status_code
        == 429
    )


def test_bearer_can_be_disabled(db_path: Path) -> None:
    app = create_app(make_settings(db_path, bearer_auth_enabled=False), start_scheduler=False)
    with TestClient(app) as c:
        assert (
            c.get(
                "/api/v1/providers", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
            ).status_code
            == 401
        )
        session_client(c)
        assert c.get("/api/v1/providers").status_code == 200
    app.state.engine.dispose()


# --- host, size, malformed, injection -------------------------------------------


def test_unknown_host_header_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/health", headers={"Host": "attacker.example"}).status_code == 400


def test_request_body_size_limit(client: TestClient, owner_headers: dict) -> None:
    big = {**PAYMENT, "notes": "x" * 70_000}
    resp = client.post("/api/v1/providers/openai/payments", json=big, headers=owner_headers)
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "request_too_large"

    def chunks() -> Iterator[bytes]:  # streamed without Content-Length
        yield b'{"notes": "'
        for _ in range(80):
            yield b"y" * 1024
        yield b'"}'

    streamed = client.post(
        "/api/v1/providers/openai/payments",
        content=chunks(),
        headers={**owner_headers, "Content-Type": "application/json"},
    )
    assert streamed.status_code == 413


@pytest.mark.parametrize(
    "q", ["' OR 1=1 --", "%", "_", "\\", "'; DROP TABLE payment_transactions; --"]
)
def test_search_injection_is_inert(client: TestClient, owner_headers: dict, q: str) -> None:
    client.post(
        "/api/v1/providers/openai/payments",
        json={**PAYMENT, "payment_reference": "INV-1"},
        headers=owner_headers,
    )
    resp = client.get("/api/v1/payments", params={"q": q}, headers=owner_headers)
    assert resp.status_code == 200 and resp.json()["total"] == 0
    assert (
        client.get("/api/v1/payments", headers=owner_headers).json()["total"] == 1
    )  # table intact


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/v1/providers/openai'%20OR%20'1'='1", None),
        ("get", "/api/v1/payments?sort=amount;DROP", None),
        ("get", "/api/v1/payments?limit=100000", None),
        ("get", "/api/v1/payments?offset=99999999", None),
        ("post", "/api/v1/providers/openai/payments", {**PAYMENT, "amount": {"$gt": 0}}),
        ("post", "/api/v1/providers/openai/payments", {**PAYMENT, "amount": ["1"]}),
        ("post", "/api/v1/providers/openai/payments", {**PAYMENT, "is_admin": True}),
        ("put", "/api/v1/providers/openai/threshold", {"amount": "1e309", "unit": "USD"}),
        ("put", "/api/v1/settings", {"display_timezone": "../../etc/passwd"}),
    ],
)
def test_malformed_and_hostile_input_rejected(
    client: TestClient, owner_headers: dict, method: str, path: str, body: object
) -> None:
    resp = getattr(client, method)(
        path, headers=owner_headers, **({"json": body} if body is not None else {})
    )
    assert resp.status_code in (404, 422), resp.text
    assert "Traceback" not in resp.text


def test_wrong_content_type_rejected(client: TestClient, owner_headers: dict) -> None:
    resp = client.post(
        "/api/v1/providers/openai/payments",
        content=b"amount=10",
        headers={**owner_headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 422


# --- leakage -------------------------------------------------------------------


SECRETS = {
    "openai_admin_key": "sk-admin-LEAKCHECK-0123456789abcdef",
    "elevenlabs_api_key": "el-LEAKCHECK-0123456789abcdef0123",
    "smtp_password": "smtp-LEAKCHECK-0123456789",
}


def test_no_secret_reaches_db_responses_or_logs(
    tmp_path: Path, migrated_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "leak.db"
    shutil.copyfile(migrated_template, db)
    settings = make_settings(
        db,
        smtp_host="127.0.0.1",
        smtp_from="m@example.com",
        smtp_username="mailer",
        **{k: SecretStr(v) for k, v in SECRETS.items()},
    )
    app = create_app(settings, start_scheduler=False)
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG), TestClient(app) as c:
        headers = session_client(c)
        bodies.append(login(c, "wrong").text)
        for path in (
            "/api/v1/health",
            "/api/v1/ready",
            "/api/v1/providers",
            "/api/v1/notifications/status",
            "/api/v1/admin/operations",
            "/api/v1/auth/session",
            "/api/v1/nope",
        ):
            bodies.append(c.get(path).text)
        bodies.append(
            c.post("/api/v1/providers/openai/payments", json=PAYMENT, headers=headers).text
        )
        bodies.append(
            c.put(
                "/api/v1/providers/openai/threshold",
                json={"amount": "5.00", "unit": "USD"},
                headers=headers,
            ).text
        )
    app.state.engine.dispose()
    every_secret = [*SECRETS.values(), OWNER_TOKEN, VIEWER_TOKEN]
    for body in bodies:
        for secret in every_secret:
            assert secret not in body
    for secret in every_secret:
        assert secret not in caplog.text
    conn = sqlite3.connect(db)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    for secret in every_secret:
        assert secret not in dump, "a credential was written to the database"


def test_smtp_errors_do_not_echo_upstream_text() -> None:
    from app.notifications.smtp import classify_smtp_error

    err = classify_smtp_error(
        smtplib.SMTPResponseException(451, b"relay.internal.corp rejected user bob")
    )
    assert "relay.internal" not in err.message and "bob" not in err.message


def test_storage_full_is_reported_explicitly(app: FastAPI, owner_headers: dict) -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/full")
    def full() -> None:
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database or disk is full"))

    app.include_router(router)
    with TestClient(app) as c:
        resp = c.get("/full")
    assert resp.status_code == 507 and resp.json()["error"]["code"] == "storage_full"


# --- admin, ops, locks ----------------------------------------------------------------------------


def test_admin_endpoints_owner_only(
    client: TestClient, viewer_headers: dict, owner_headers: dict
) -> None:
    for method, path in (
        ("get", "/api/v1/admin/operations"),
        ("post", "/api/v1/admin/backup"),
        ("post", "/api/v1/admin/sessions/revoke-all"),
    ):
        assert getattr(client, method)(path, headers=viewer_headers).status_code == 403
        assert getattr(client, method)(path).status_code == 401
    ops = client.get("/api/v1/admin/operations", headers=owner_headers).json()
    assert {"status", "disk_free_mb", "backup", "permission_findings", "scheduler"} <= set(ops)


def test_backup_is_verified_recorded_and_pruned(
    tmp_path: Path, migrated_template: Path, owner_headers: dict
) -> None:
    db = tmp_path / "b.db"
    shutil.copyfile(migrated_template, db)
    backups = tmp_path / "backups"
    app = create_app(make_settings(db, backup_dir=backups, backup_keep=2), start_scheduler=False)
    with TestClient(app) as c:
        for _ in range(3):
            assert (
                c.post("/api/v1/admin/backup", headers=owner_headers).json()["status"]
                == "succeeded"
            )
            import time

            time.sleep(1.1)  # distinct timestamped file names
        ops = c.get("/api/v1/admin/operations", headers=owner_headers).json()
    app.state.engine.dispose()
    files = sorted(backups.glob("monitor-*.db"))
    assert len(files) == 2  # retention
    conn = sqlite3.connect(files[-1])
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM providers").fetchone()[0] == 5
    finally:
        conn.close()
    assert ops["backup"]["last_status"] == "succeeded" and not ops["backup"]["overdue"]


def test_restored_backup_is_healthy_and_unlocked(
    tmp_path: Path, migrated_template: Path, owner_headers: dict
) -> None:
    """A backup is taken while its own run (and often the alert cycle) holds a lock. The copy
    must not carry that runtime state: restoring it must not report a failed backup or
    block the next one until stale locks expire (found in the deployment rehearsal)."""
    db = tmp_path / "live.db"
    shutil.copyfile(migrated_template, db)
    backups = tmp_path / "backups"
    app = create_app(make_settings(db, backup_dir=backups), start_scheduler=False)
    with TestClient(app) as c:
        assert app.state.operations.locks.acquire("alert-cycle", timedelta(minutes=10))
        assert c.post("/api/v1/admin/backup", headers=owner_headers).json()["status"] == "succeeded"
    app.state.engine.dispose()
    [backup] = backups.glob("monitor-*.db")

    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("SELECT count(*) FROM job_locks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM monitoring_runs WHERE job_name = 'backup'"
        ).fetchall() == [("succeeded",)]
    finally:
        conn.close()

    restored = tmp_path / "restored.db"
    shutil.copyfile(backup, restored)
    app2 = create_app(
        make_settings(restored, backup_dir=tmp_path / "backups2"), start_scheduler=False
    )
    with TestClient(app2) as c:
        ops = c.get("/api/v1/admin/operations", headers=owner_headers).json()
        assert ops["backup"]["last_status"] == "succeeded" and not ops["backup"]["overdue"]
        assert not any("backup" in p.lower() for p in ops["problems"])
        again = c.post("/api/v1/admin/backup", headers=owner_headers).json()
        assert again["status"] == "succeeded"  # not "a backup is already running"
    app2.state.engine.dispose()


def test_backup_failure_is_visible(
    tmp_path: Path, migrated_template: Path, owner_headers: dict
) -> None:
    db = tmp_path / "f.db"
    shutil.copyfile(migrated_template, db)
    # Demand more free space than any disk has: the backup must fail loudly, not silently.
    app = create_app(
        make_settings(db, backup_dir=tmp_path / "bk", disk_min_free_mb=999_999),
        start_scheduler=False,
    )
    with TestClient(app) as c:
        result = c.post("/api/v1/admin/backup", headers=owner_headers).json()
        ops = c.get("/api/v1/admin/operations", headers=owner_headers).json()
        ready = c.get("/api/v1/ready")
    app.state.engine.dispose()
    assert result["status"] == "failed" and "insufficient disk space" in result["error"]
    assert any("Last backup failed" in p for p in ops["problems"])
    assert ops["status"] == "critical"  # disk below the minimum
    assert ready.status_code == 503 and ready.json()["checks"]["disk"] == "failed"


def test_job_lease_lock(app: FastAPI) -> None:
    a = JobLocks(app.state.session_factory, owner="process-a")
    b = JobLocks(app.state.session_factory, owner="process-b")
    assert a.acquire("poll:openai", timedelta(minutes=5)) is True
    assert b.acquire("poll:openai", timedelta(minutes=5)) is False
    b.release("poll:openai")  # not the holder: no effect
    assert b.acquire("poll:openai", timedelta(minutes=5)) is False
    a.release("poll:openai")
    assert b.acquire("poll:openai", timedelta(minutes=5)) is True
    # An expired lease (crashed holder) is taken over.
    assert a.acquire("expiring", timedelta(seconds=-1)) is True
    assert b.acquire("expiring", timedelta(minutes=5)) is True


def test_poll_skipped_when_another_process_holds_the_lease(app: FastAPI) -> None:
    from tests.test_polling import coordinator, deepgram_fake, deepgram_runtime

    other = JobLocks(app.state.session_factory, owner="other-process")
    assert other.acquire("poll:deepgram", timedelta(minutes=5))
    coord = coordinator(app, {"deepgram": deepgram_runtime(deepgram_fake())})
    summary = asyncio.run(coord.run_provider("deepgram"))
    assert summary.skipped and summary.in_progress
    assert "another process" in (summary.reason or "")


def test_cancelled_poll_is_recorded_and_unlocked(app: FastAPI) -> None:
    from app.providers.base import CapabilityInfo, ProviderAdapter
    from app.providers.registry import ProviderRuntime
    from app.providers.types import Operation, OperationResult, SyncStatus
    from tests.test_polling import coordinator

    class Hanging(ProviderAdapter):
        slug = "deepgram"
        capabilities = {Operation.HEALTH: CapabilityInfo(True, "test")}

        async def _health(self) -> OperationResult:
            await asyncio.sleep(30)
            return self.result(Operation.HEALTH)

    coord = coordinator(
        app,
        {
            "deepgram": ProviderRuntime(
                "deepgram", Hanging(), SyncStatus.OK, capabilities=dict(Hanging.capabilities)
            )
        },
    )

    def run_started() -> bool:
        s = app.state.session_factory()
        try:
            return s.scalar(select(func.count()).select_from(MonitoringRun)) == 1
        finally:
            s.close()

    async def scenario() -> None:
        task = asyncio.create_task(coord.run_provider("deepgram"))
        # Cancel only once the run row exists (a fixed sleep was flaky under load; a cancel
        # before that point is covered by startup recovery of interrupted runs).
        for _ in range(500):
            await asyncio.sleep(0.01)
            if await asyncio.to_thread(run_started):
                break
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    s = app.state.session_factory()
    try:
        run = s.scalars(select(MonitoringRun)).one()
        assert run.status is RunStatus.FAILED and "cancelled" in (run.error_message or "")
        assert s.execute(text("SELECT count(*) FROM job_locks")).scalar() == 0  # lease released
    finally:
        s.close()


@pytest.mark.parametrize("q", ["\u0000", "inv\u0000", "a\nb", "\u007f"])
def test_search_control_characters_rejected(
    client: TestClient, owner_headers: dict, q: str
) -> None:
    # NUL would truncate the LIKE pattern inside SQLite and match every row.
    resp = client.get("/api/v1/payments", params={"q": q}, headers=owner_headers)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_search"
