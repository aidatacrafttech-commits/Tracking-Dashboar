from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import ProviderBalanceSnapshot, ProviderUsageSnapshot
from app.models.enums import BalanceKind, UnitType, ValueSource
from tests.conftest import provider_by_slug


def test_list_providers(client: TestClient, viewer_headers: dict) -> None:
    resp = client.get("/api/v1/providers", headers=viewer_headers)
    assert resp.status_code == 200
    providers = {p["id"]: p for p in resp.json()}
    assert set(providers) == {"openai", "sarvam", "deepgram", "elevenlabs", "aws"}
    assert providers["deepgram"]["balance_capability"] == "api"
    assert providers["openai"]["balance_capability"] == "manual_only"
    assert providers["aws"]["balance_capability"] == "postpaid"
    assert providers["openai"]["latest_balance"] is None  # unknown, not zero
    assert providers["openai"]["threshold"] is None
    assert "no balance API" in providers["openai"]["balance_capability_note"]


def test_provider_detail_with_latest_balance_and_freshness(
    client: TestClient, viewer_headers: dict, session: Session
) -> None:
    deepgram = provider_by_slug(session, "deepgram")
    now = utcnow()
    session.add_all(
        [
            ProviderBalanceSnapshot(
                provider_id=deepgram.id,
                balance_kind=BalanceKind.PREPAID_BALANCE,
                amount=Decimal("120.5"),
                unit="USD",
                unit_type=UnitType.CURRENCY,
                source=ValueSource.PROVIDER_API,
                observed_at=now - timedelta(hours=5),
            ),
            ProviderBalanceSnapshot(
                provider_id=deepgram.id,
                balance_kind=BalanceKind.PREPAID_BALANCE,
                amount=Decimal("118.25"),
                unit="USD",
                unit_type=UnitType.CURRENCY,
                source=ValueSource.PROVIDER_API,
                observed_at=now - timedelta(hours=3),
            ),
        ]
    )
    session.commit()

    data = client.get("/api/v1/providers/deepgram", headers=viewer_headers).json()
    assert data["latest_balance"]["amount"] == "118.25"
    assert data["latest_balance"]["source"] == "provider_api"
    freshness = data["latest_balance_freshness"]
    assert 3 * 3600 - 5 <= freshness["age_seconds"] <= 3 * 3600 + 60
    assert freshness["is_stale"] is True  # deepgram stale_after = 120 min


def test_unavailable_balance_is_null_with_reason(
    client: TestClient, viewer_headers: dict, session: Session
) -> None:
    openai = provider_by_slug(session, "openai")
    session.add(
        ProviderBalanceSnapshot(
            provider_id=openai.id,
            balance_kind=BalanceKind.PREPAID_BALANCE,
            amount=None,
            unit="USD",
            unit_type=UnitType.CURRENCY,
            source=ValueSource.PROVIDER_API,
            observed_at=utcnow(),
            unavailable_reason="No official balance endpoint",
        )
    )
    session.commit()
    data = client.get("/api/v1/providers/openai", headers=viewer_headers).json()
    assert data["latest_balance"]["amount"] is None
    assert data["latest_balance"]["unavailable_reason"] == "No official balance endpoint"


def test_snapshots_balance_and_usage(
    client: TestClient, viewer_headers: dict, session: Session
) -> None:
    aws = provider_by_slug(session, "aws")
    day = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(3):
        session.add(
            ProviderUsageSnapshot(
                provider_id=aws.id,
                metric="cost",
                dimension="Amazon S3",
                period_start=day + timedelta(days=i),
                period_end=day + timedelta(days=i + 1),
                cost_amount=Decimal("1.234567") * (i + 1),
                cost_currency="USD",
                source=ValueSource.BILLING_REPORT,
                observed_at=day + timedelta(days=i + 1, hours=8),
            )
        )
    session.add(
        ProviderBalanceSnapshot(
            provider_id=aws.id,
            balance_kind=BalanceKind.PROMOTIONAL_CREDIT,
            amount=Decimal("300"),
            unit="USD",
            unit_type=UnitType.CURRENCY,
            source=ValueSource.BILLING_REPORT,
            observed_at=day,
            data_as_of=day - timedelta(hours=20),
        )
    )
    session.commit()

    base = "/api/v1/providers/aws/snapshots"
    balance = client.get(base, headers=viewer_headers).json()
    assert balance["kind"] == "balance" and balance["total"] == 1
    assert balance["items"][0]["data_as_of"] == "2026-09-30T04:00:00Z"

    usage = client.get(base, params={"kind": "usage"}, headers=viewer_headers).json()
    assert usage["kind"] == "usage" and usage["total"] == 3
    assert usage["items"][0]["cost_amount"] == "3.703701"  # newest period first, exact
    assert usage["items"][0]["is_final"] is False

    ranged = client.get(
        base,
        params={"kind": "usage", "start": "2026-10-02T00:00:00Z", "end": "2026-10-03T00:00:00Z"},
        headers=viewer_headers,
    ).json()
    assert ranged["total"] == 1

    bad = client.get(base, params={"kind": "invoices"}, headers=viewer_headers)
    assert bad.status_code == 422
    bad = client.get(
        base,
        params={"start": "2026-10-03T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
        headers=viewer_headers,
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_range"


def test_unknown_provider_404(client: TestClient, viewer_headers: dict) -> None:
    for path in ("/api/v1/providers/unknown", "/api/v1/providers/unknown/snapshots"):
        resp = client.get(path, headers=viewer_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "provider_not_found"
