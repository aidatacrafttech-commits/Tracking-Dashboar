"""Sarvam AI: no documented API for credits, usage/spend, workspace usage or payment history
(docs.sarvam.ai/api/platform/billing and /faq, checked 2026-10-15). These tests pin down the
supported path instead: the public status page only, a manually entered INR balance, and the
existing threshold engine on top of it. No Sarvam key is ever read or sent."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.enums import ValueSource
from app.providers.registry import build_runtimes
from app.providers.sarvam import SarvamAdapter
from app.providers.types import ErrorCode, Operation, SyncStatus
from tests.conftest import make_settings
from tests.fakes import STATUS_OK, FakeHttp, SleepRecorder
from tests.test_phase4_api import snapshot

STATUS = "https://status.sarvam.ai/api/v2/status.json"
P = "/api/v1/providers/sarvam"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def adapter(fake: FakeHttp) -> SarvamAdapter:
    return SarvamAdapter(transport=fake.transport, sleep=SleepRecorder())


def test_only_the_public_status_page_is_contacted_without_any_key() -> None:
    fake = FakeHttp().json(STATUS, STATUS_OK)
    a = adapter(fake)
    async def both():  # type: ignore[no-untyped-def]
        try:
            return [await a.fetch_balance(), await a.health_check()]
        finally:
            await a.aclose()

    results = run(both())
    assert results[0].status is SyncStatus.UNSUPPORTED and results[0].metrics == ()
    assert results[1].status is SyncStatus.OK
    assert [r.url.host for r in fake.requests] == ["status.sarvam.ai"]
    sent = {k.lower() for r in fake.requests for k in r.headers}
    assert not sent & {"api-subscription-key", "authorization"}


def test_capability_notes_point_to_the_official_dashboard_locations() -> None:
    caps = SarvamAdapter.capabilities
    assert not caps[Operation.BALANCE].supported
    assert 'Settings -> Billing ("Credits Left")' in caps[Operation.BALANCE].note
    assert "Settings -> Usage" in caps[Operation.USAGE].note
    assert "Payment History" in caps[Operation.BILLING].note


def test_status_page_rate_limit_is_a_safe_error() -> None:
    fake = FakeHttp().add("GET", STATUS, httpx.Response(429, headers={"Retry-After": "3600"}))
    a = adapter(fake)
    result = run(a.health_check())
    run(a.aclose())
    assert result.status is SyncStatus.ERROR
    assert result.error is not None and result.error.code is ErrorCode.RATE_LIMITED


def test_sarvam_key_in_env_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sarvam-key-must-not-be-used-0123456789"
    monkeypatch.setenv("SARVAM_API_KEY", secret)
    settings = make_settings(tmp_path / "x.db")
    assert secret not in str(settings.model_dump())
    runtime = build_runtimes(settings)["sarvam"]
    assert runtime.status is SyncStatus.OK and runtime.missing_config == ()


def test_manual_inr_balance_drives_threshold_alerts(
    client: TestClient, owner_headers: dict
) -> None:
    resp = client.put(
        f"{P}/threshold",
        json={"amount": "500.00", "unit": "INR", "eligible_sources": ["manual"]},
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.text

    def enter(amount: str) -> None:
        r = client.post(
            f"{P}/balance-entries", json={"amount": amount, "unit": "INR"}, headers=owner_headers
        )
        assert r.status_code == 201, r.text

    def events() -> list[str]:
        items = client.get(f"{P}/alerts", headers=owner_headers).json()["items"]
        return [a["event_type"] for a in sorted(items, key=lambda a: a["id"])]

    enter("1581.68")
    assert events() == []
    enter("450.00")  # below the threshold: exactly one alert
    enter("300.00")  # still low: no duplicate
    assert events() == ["breached"]
    enter("2000.00")  # topped up: one recovery notice
    assert events() == ["breached", "recovered"]
    card = client.get(P, headers=owner_headers).json()
    assert (card["latest_balance"]["amount"], card["latest_balance"]["source_type"]) == (
        "2000",
        "manual",
    )


def test_stale_manual_balance_never_alerts(
    client: TestClient, owner_headers: dict, session: Session
) -> None:
    client.put(f"{P}/threshold", json={"amount": "500.00", "unit": "INR"}, headers=owner_headers)
    # Sarvam's manual balance is stale after 24 h (stale_after_minutes=1440).
    snapshot(
        session,
        "sarvam",
        "100",
        unit="INR",
        source=ValueSource.MANUAL,
        at=utcnow() - timedelta(hours=25),
    )
    result = client.post(f"{P}/threshold/evaluate", headers=owner_headers).json()
    assert result["status"] == "stale"
    assert client.get(f"{P}/alerts", headers=owner_headers).json()["items"] == []
    card = client.get(P, headers=owner_headers).json()
    assert card["latest_balance_freshness"]["is_stale"] is True
