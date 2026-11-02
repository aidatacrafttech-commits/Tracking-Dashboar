from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.registry import build_runtimes
from tests.fakes import STATUS_OK, FakeHttp
from tests.test_polling import deepgram_fake, deepgram_runtime

DG_KEY = "dg-key-0123456789abcdef0123"


@pytest.fixture
def fake() -> FakeHttp:
    return deepgram_fake()


@pytest.fixture
def wired_app(settings: Settings, fake: FakeHttp) -> Iterator[FastAPI]:
    runtimes = build_runtimes(settings)  # openai/elevenlabs not configured, aws disabled
    runtimes["deepgram"] = deepgram_runtime(fake)
    application = create_app(settings, runtimes=runtimes, start_scheduler=False)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(wired_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(wired_app) as c:
        yield c


def test_provider_list_shows_integration_status(api: TestClient, viewer_headers: dict) -> None:
    providers = {p["id"]: p for p in api.get("/api/v1/providers", headers=viewer_headers).json()}
    openai = providers["openai"]["integration"]
    assert openai["status"] == "not_configured"
    assert openai["missing_config"] == ["OPENAI_ADMIN_KEY"]
    caps = {c["operation"]: c for c in openai["capabilities"]}
    assert caps["balance"]["supported"] is False
    assert providers["aws"]["integration"]["status"] == "disabled"
    assert providers["deepgram"]["integration"]["status"] == "ok"


def test_manual_sync_then_status_and_data(
    api: TestClient, owner_headers: dict, viewer_headers: dict
) -> None:
    resp = api.post("/api/v1/providers/deepgram/sync", headers=owner_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded" and body["records_written"] > 0
    assert {o["operation"] for o in body["operations"]} == {"health", "balance", "usage", "billing"}

    status = api.get("/api/v1/providers/deepgram/sync-status", headers=viewer_headers).json()
    ops = {o["operation"]: o for o in status["operations"]}
    assert ops["balance"]["status"] == "ok"
    assert ops["balance"]["last_successful_sync"] is not None
    assert ops["balance"]["is_stale"] is False

    provider = api.get("/api/v1/providers/deepgram", headers=viewer_headers).json()
    assert provider["latest_balance"]["amount"] == "1250.75"
    assert provider["latest_balance"]["source_type"] == "api_verified"
    assert provider["latest_balance"]["data_latency"] == "realtime"

    usage = api.get(
        "/api/v1/providers/deepgram/snapshots",
        params={"kind": "usage", "limit": 500},
        headers=viewer_headers,
    ).json()
    cost = next(i for i in usage["items"] if i["metric"] == "cost")
    assert cost["source_type"] == "api_verified" and cost["data_latency"] == "delayed"

    payments = api.get("/api/v1/providers/deepgram/payments", headers=viewer_headers).json()
    assert [p["entry_method"] for p in payments["items"]] == ["provider_verified"]

    health = api.get("/api/v1/providers/deepgram/health-checks", headers=viewer_headers).json()
    assert health[0]["status"] == "operational"


def test_manual_sync_rate_limited(api: TestClient, owner_headers: dict) -> None:
    assert api.post("/api/v1/providers/deepgram/sync", headers=owner_headers).status_code == 200
    resp = api.post("/api/v1/providers/deepgram/sync", headers=owner_headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "sync_rate_limited"


def test_sync_requires_owner(api: TestClient, viewer_headers: dict) -> None:
    assert api.post("/api/v1/providers/deepgram/sync", headers=viewer_headers).status_code == 403
    assert api.post("/api/v1/providers/deepgram/sync").status_code == 401


def test_sync_not_configured_provider(api: TestClient, owner_headers: dict) -> None:
    body = api.post("/api/v1/providers/openai/sync", headers=owner_headers).json()
    assert body["skipped"] is True and "OPENAI_ADMIN_KEY" in body["reason"]
    status = api.get("/api/v1/providers/openai/sync-status", headers=owner_headers).json()
    assert {o["status"] for o in status["operations"]} == {"not_configured"}


def test_failed_sync_keeps_last_value_and_reports_error(
    api: TestClient, owner_headers: dict, fake: FakeHttp, wired_app: FastAPI
) -> None:
    import httpx

    api.post("/api/v1/providers/deepgram/sync", headers=owner_headers)
    fake.add(
        "GET",
        "https://api.deepgram.com/v1/projects/proj-1/balances",
        httpx.Response(503, json={"err": "down"}),
    )
    wired_app.state.coordinator._last_manual.clear()  # bypass the cooldown for the test
    body = api.post("/api/v1/providers/deepgram/sync", headers=owner_headers).json()
    assert body["status"] == "partial"
    balance_op = next(o for o in body["operations"] if o["operation"] == "balance")
    assert balance_op["error_code"] == "upstream_error"

    provider = api.get("/api/v1/providers/deepgram", headers=owner_headers).json()
    assert provider["latest_balance"]["amount"] == "1250.75"  # last good value kept
    status = api.get("/api/v1/providers/deepgram/sync-status", headers=owner_headers).json()
    ops = {o["operation"]: o for o in status["operations"]}
    assert ops["balance"]["status"] == "error"
    assert ops["balance"]["consecutive_failures"] == 1
    assert ops["balance"]["last_successful_sync"] is not None


def test_manual_balance_entry(api: TestClient, owner_headers: dict, viewer_headers: dict) -> None:
    url = "/api/v1/providers/openai/balance-entries"
    body = {
        "amount": "84.20",
        "unit": "USD",
        "observed_at": "2026-10-12T09:00:00+05:30",
        "notes": "from platform.openai.com billing page",
    }
    assert api.post(url, json=body, headers=viewer_headers).status_code == 403
    resp = api.post(url, json=body, headers=owner_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["source"] == "manual" and data["source_type"] == "manual"
    assert data["observed_at"] == "2026-10-12T03:30:00Z"

    provider = api.get("/api/v1/providers/openai", headers=viewer_headers).json()
    assert provider["latest_balance"]["amount"] == "84.2"


@pytest.mark.parametrize(
    ("slug", "body", "code"),
    [
        ("openai", {"amount": "10", "unit": "INR"}, "unit_mismatch"),
        ("openai", {"amount": "10.001", "unit": "USD"}, "invalid_precision"),
        (
            "sarvam",
            {"amount": "10", "unit": "INR", "balance_kind": "plan_quota"},
            "unsupported_balance_kind",
        ),
        (
            "elevenlabs",
            {"amount": "10", "unit": "XYZ", "balance_kind": "prepaid_balance"},
            "unsupported_currency",
        ),
        (
            "openai",
            {"amount": "10", "unit": "USD", "observed_at": "2099-01-01T00:00:00Z"},
            "future_observed_at",
        ),
    ],
)
def test_manual_balance_validation(
    api: TestClient, owner_headers: dict, slug: str, body: dict, code: str
) -> None:
    resp = api.post(f"/api/v1/providers/{slug}/balance-entries", json=body, headers=owner_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_elevenlabs_accepts_money_balance_and_credit_quota(
    api: TestClient, owner_headers: dict
) -> None:
    url = "/api/v1/providers/elevenlabs/balance-entries"
    money = api.post(
        url,
        json={"amount": "12.50", "unit": "USD", "balance_kind": "prepaid_balance"},
        headers=owner_headers,
    )
    assert money.status_code == 201 and money.json()["unit_type"] == "currency"
    credits = api.post(url, json={"amount": "9000", "unit": "CREDITS"}, headers=owner_headers)
    assert credits.status_code == 201 and credits.json()["unit_type"] == "credits"


def test_aws_resources_empty_and_stale(api: TestClient, viewer_headers: dict) -> None:
    data = api.get("/api/v1/aws/resources", headers=viewer_headers).json()
    assert data == {"items": [], "observed_at": None, "is_stale": True}


def test_secrets_never_reach_responses_or_logs(
    settings: Settings, caplog: pytest.LogCaptureFixture, owner_headers: dict
) -> None:
    secret = "dg-SUPERSECRET-key-0123456789abcdef"
    fake = FakeHttp()
    fake.json(
        "https://api.deepgram.com/v1/projects/proj-1/balances",
        {"err_msg": f"Invalid credentials: {secret}"},
        status=401,
    )
    fake.json("https://status.deepgram.com/api/v2/status.json", STATUS_OK)
    keyed = settings.model_copy(update={"deepgram_api_key": None})
    runtimes = build_runtimes(keyed)
    from app.providers.deepgram import DeepgramAdapter
    from app.providers.registry import ProviderRuntime
    from app.providers.types import SyncStatus

    runtimes["deepgram"] = ProviderRuntime(
        "deepgram",
        DeepgramAdapter(secret, "proj-1", transport=fake.transport, max_retries=0),
        SyncStatus.OK,
        capabilities=dict(DeepgramAdapter.capabilities),
    )
    application = create_app(settings, runtimes=runtimes, start_scheduler=False)
    with caplog.at_level(logging.DEBUG), TestClient(application) as c:
        texts = [
            c.post("/api/v1/providers/deepgram/sync", headers=owner_headers).text,
            c.get("/api/v1/providers/deepgram/sync-status", headers=owner_headers).text,
            c.get("/api/v1/providers", headers=owner_headers).text,
        ]
    application.state.engine.dispose()
    [balance_call] = fake.calls("/v1/projects/proj-1/balances")
    assert balance_call.headers["Authorization"] == f"Token {secret}"  # it *was* sent upstream
    for text in texts:
        assert secret not in text
    assert secret not in caplog.text
