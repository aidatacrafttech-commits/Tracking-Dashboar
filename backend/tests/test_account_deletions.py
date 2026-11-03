"""Account Deletions BFF: the dashboard server calls the AdhyaapakAI admin API with the
server-side secret; browsers never see it. No real network I/O (httpx MockTransport)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.main import create_app
from app.models import AuditLog
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings
from tests.fakes import FakeHttp

SECRET = "adhyaapak-admin-secret-for-tests-only"
UP = "http://adhyaapak.test"
D = "/api/v1/account-deletions"
OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
LIST = {
    "count": 2,
    "grace_days": 7,
    "deletions": [
        {
            "user_id": "u1",
            "email": "amit@example.com",
            "name": "Amit",
            "requested_at": "2026-11-01T05:00:00Z",
            "due_at": "2026-11-08T05:00:00Z",
            "days_left": 1,
        },
        {
            "user_id": "u2",
            "email": "neha@example.com",
            "name": None,
            "requested_at": "2026-11-02T05:00:00Z",
            "due_at": "2026-11-09T05:00:00Z",
            "days_left": 2,
        },
    ],
}


@pytest.fixture
def fake() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def dapp(db_path: Path, fake: FakeHttp) -> Iterator[FastAPI]:
    application = create_app(
        make_settings(db_path, adhyaapak_api_base_url=UP, adhyaapak_admin_secret=SecretStr(SECRET)),
        start_scheduler=False,
    )
    application.state.adhyaapak_transport = fake.transport
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(dapp: FastAPI) -> Iterator[TestClient]:
    with TestClient(dapp) as c:
        yield c


def error(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def test_list_goes_through_the_server_with_the_secret(api: TestClient, fake: FakeHttp) -> None:
    fake.json(f"{UP}/api/admin/accounts/deletions", LIST)
    resp = api.get(D, headers=VIEWER)  # viewers may look
    assert resp.status_code == 200
    body = resp.json()
    assert (body["count"], body["grace_days"], body["deletions"][0]["email"]) == (
        2,
        7,
        "amit@example.com",
    )
    assert api.get(f"{D}/status", headers=VIEWER).json() == {
        "configured": True,
        "target": "adhyaapak.test",
    }
    sent = fake.calls("/api/admin/accounts/deletions")[0]
    assert sent.headers["x-admin-secret"] == SECRET
    assert SECRET not in resp.text and "x-admin-secret" not in resp.text.lower()


def test_wrong_secret_reports_not_authorised(api: TestClient, fake: FakeHttp) -> None:
    fake.json(f"{UP}/api/admin/accounts/deletions", {"detail": "Forbidden."}, status=403)
    resp = api.get(D, headers=OWNER)
    assert resp.status_code == 502 and error(resp) == "adhyaapak_forbidden"
    assert "Not authorised" in resp.json()["error"]["message"]


def test_not_configured_and_unreachable(db_path: Path, fake: FakeHttp) -> None:
    application = create_app(make_settings(db_path), start_scheduler=False)
    with TestClient(application) as c:
        assert c.get(f"{D}/status", headers=VIEWER).json() == {"configured": False, "target": None}
        resp = c.get(D, headers=OWNER)
        assert resp.status_code == 503 and error(resp) == "adhyaapak_not_configured"
    application.state.engine.dispose()
    application = create_app(
        make_settings(db_path, adhyaapak_api_base_url=UP, adhyaapak_admin_secret=SecretStr(SECRET)),
        start_scheduler=False,
    )
    fake.add("GET", f"{UP}/api/admin/accounts/deletions", httpx.ConnectError("refused"))
    application.state.adhyaapak_transport = fake.transport
    with TestClient(application) as c:
        resp = c.get(D, headers=OWNER)
        assert resp.status_code == 502 and error(resp) == "adhyaapak_unreachable"
    application.state.engine.dispose()


def test_restore_requires_owner_and_note_and_is_audited_without_full_email(
    api: TestClient, dapp: FastAPI, fake: FakeHttp
) -> None:
    fake.json(
        f"{UP}/api/admin/accounts/reactivate",
        {
            "success": True,
            "status": "reactivated",
            "user_id": "u1",
            "email": "amit@example.com",
            "already": False,
        },
        method="POST",
    )
    body = {"email": "amit@example.com", "note": "ticket 123, mail from registered address"}
    assert api.post(f"{D}/reactivate", json=body, headers=VIEWER).status_code == 403
    assert api.post(f"{D}/reactivate", json=body).status_code == 401
    assert api.post(f"{D}/reactivate", json={**body, "note": ""}, headers=OWNER).status_code == 422
    assert (
        api.post(
            f"{D}/reactivate", json={**body, "email": "not-an-email"}, headers=OWNER
        ).status_code
        == 422
    )
    assert fake.calls("/api/admin/accounts/reactivate") == []  # nothing reached AdhyaapakAI
    resp = api.post(f"{D}/reactivate", json=body, headers=OWNER)
    assert resp.status_code == 200 and resp.json()["status"] == "reactivated"
    sent = fake.calls("/api/admin/accounts/reactivate")[0]
    assert json.loads(sent.content) == body and sent.headers["x-admin-secret"] == SECRET
    with dapp.state.session_factory() as s:
        entry = s.scalars(select(AuditLog).where(AuditLog.action == "account.reactivate")).one()
    assert entry.entity_id == "u1"
    assert entry.changes["email"] == "a***@example.com" and entry.changes["note"] == body["note"]


def test_repeat_restore_is_success(api: TestClient, fake: FakeHttp) -> None:
    fake.json(
        f"{UP}/api/admin/accounts/reactivate",
        {
            "success": True,
            "status": "reactivated",
            "user_id": "u1",
            "email": "amit@example.com",
            "already": True,
        },
        method="POST",
    )
    resp = api.post(
        f"{D}/reactivate", json={"email": "amit@example.com", "note": "ticket 9"}, headers=OWNER
    )
    assert resp.status_code == 200 and resp.json()["already"] is True


@pytest.mark.parametrize(
    ("status", "payload", "expect_status", "expect_code"),
    [
        (404, {"detail": "No account with that email."}, 404, "adhyaapak_404"),
        (
            409,
            {"detail": "Account is active; nothing to restore.", "code": "NOT_PENDING"},
            409,
            "not_pending",
        ),
        (410, {"detail": "Permanently deleted on 2026-10-30."}, 410, "adhyaapak_410"),
        (400, {"detail": "Email is required."}, 400, "adhyaapak_400"),
    ],
)
def test_restore_errors_pass_the_detail_through(
    api: TestClient,
    fake: FakeHttp,
    status: int,
    payload: dict,
    expect_status: int,
    expect_code: str,
) -> None:
    fake.json(f"{UP}/api/admin/accounts/reactivate", payload, status=status, method="POST")
    resp = api.post(
        f"{D}/reactivate", json={"email": "amit@example.com", "note": "ticket 1"}, headers=OWNER
    )
    assert resp.status_code == expect_status and error(resp) == expect_code
    assert resp.json()["error"]["message"] == payload["detail"]


def test_purge_never_sends_force_and_reports_errors(
    api: TestClient, dapp: FastAPI, fake: FakeHttp
) -> None:
    fake.add(
        "POST",
        f"{UP}/api/admin/accounts/purge-now",
        httpx.Response(409, json={"detail": "No pending deletion request for this account."}),
        httpx.Response(200, json={"success": True, "status": "completed", "user_id": "u2"}),
    )
    # `force` is not accepted from the browser at all
    forced = api.post(
        f"{D}/purge-now",
        json={"email": "neha@example.com", "note": "asked", "force": True},
        headers=OWNER,
    )
    assert forced.status_code == 422
    first = api.post(
        f"{D}/purge-now",
        json={"email": "neha@example.com", "note": "asked for immediate deletion"},
        headers=OWNER,
    )
    assert (
        first.status_code == 409
        and first.json()["error"]["message"] == "No pending deletion request for this account."
    )
    second = api.post(
        f"{D}/purge-now",
        json={"email": "neha@example.com", "note": "asked for immediate deletion"},
        headers=OWNER,
    )
    assert second.status_code == 200 and second.json()["status"] == "completed"
    assert all(
        json.loads(r.content)["force"] is False for r in fake.calls("/api/admin/accounts/purge-now")
    )
    with dapp.state.session_factory() as s:
        actions = s.scalars(
            select(AuditLog.action).where(AuditLog.entity_type == "student_account")
        ).all()
    assert actions == ["account.purge_now"]  # only the successful purge is recorded


def test_purge_failure_and_already_running(api: TestClient, fake: FakeHttp) -> None:
    fake.add(
        "POST",
        f"{UP}/api/admin/accounts/purge-now",
        httpx.Response(500, json={"detail": "Purge failed part-way; the sweeper will retry."}),
        httpx.Response(
            200,
            json={
                "success": True,
                "status": "pending",
                "message": "Purge already running; the sweeper will finish it.",
            },
        ),
    )
    failed = api.post(
        f"{D}/purge-now", json={"email": "neha@example.com", "note": "asked"}, headers=OWNER
    )
    assert failed.status_code == 502 and "sweeper will retry" in failed.json()["error"]["message"]
    pending = api.post(
        f"{D}/purge-now", json={"email": "neha@example.com", "note": "asked"}, headers=OWNER
    )
    assert pending.status_code == 200 and pending.json()["status"] == "pending"


def test_cookie_sessions_need_csrf_for_actions(api: TestClient, fake: FakeHttp) -> None:
    fake.json(
        f"{UP}/api/admin/accounts/reactivate",
        {"success": True, "status": "reactivated"},
        method="POST",
    )
    origin = {"Origin": "http://testserver"}
    login = api.post("/api/v1/auth/login", json={"token": OWNER_TOKEN}, headers=origin)
    body = {"email": "amit@example.com", "note": "ticket 5"}
    assert api.post(f"{D}/reactivate", json=body, headers=origin).status_code == 403
    ok = api.post(
        f"{D}/reactivate", json=body, headers={**origin, "X-CSRF-Token": login.json()["csrf_token"]}
    )
    assert ok.status_code == 200


def test_student_emails_are_never_logged(
    api: TestClient, fake: FakeHttp, caplog: pytest.LogCaptureFixture
) -> None:
    fake.json(f"{UP}/api/admin/accounts/deletions", LIST)
    fake.json(
        f"{UP}/api/admin/accounts/reactivate",
        {"detail": "Account is active."},
        status=409,
        method="POST",
    )
    with caplog.at_level(logging.DEBUG):
        api.get(D, headers=OWNER)
        api.post(
            f"{D}/reactivate", json={"email": "amit@example.com", "note": "ticket 7"}, headers=OWNER
        )
    text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "amit@example.com" not in text and "neha@example.com" not in text and SECRET not in text
