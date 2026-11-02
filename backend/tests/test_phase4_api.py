"""Phase 4 backend: session role, payment search/edit/delete, explicit reconciliation,
overview aggregates, usage series, threshold headroom, SSE change stream, UI serving."""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.main import create_app
from app.models import AuditLog, PaymentTransaction, ProviderBalanceSnapshot, ProviderUsageSnapshot
from app.models.enums import (
    Actor,
    BalanceKind,
    EntryMethod,
    PaymentKind,
    UnitType,
    ValueSource,
)
from tests.conftest import make_settings, provider_by_slug

NOW = utcnow()


def pay(client: TestClient, headers: dict, slug: str = "openai", **kw) -> dict:  # type: ignore[no-untyped-def]
    body = {
        "amount": "50.00",
        "currency": "USD",
        "transaction_date": (NOW - timedelta(days=2)).isoformat(),
        "kind": "top_up",
    }
    body.update(kw)
    resp = client.post(f"/api/v1/providers/{slug}/payments", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def manual_balance(
    client: TestClient, headers: dict, slug: str, amount: str, unit: str, observed_at: datetime
) -> dict:
    resp = client.post(
        f"/api/v1/providers/{slug}/balance-entries",
        headers=headers,
        json={"amount": amount, "unit": unit, "observed_at": observed_at.isoformat()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- session ---------------------------------------------------------------------------------


def test_session_role(client, owner_headers, viewer_headers) -> None:  # type: ignore[no-untyped-def]
    owner = client.get("/api/v1/auth/session", headers=owner_headers).json()
    assert (owner["role"], owner["can_write"], owner["via"]) == ("owner", True, "bearer")
    assert owner["csrf_token"] is None  # bearer requests need no CSRF token
    viewer = client.get("/api/v1/auth/session", headers=viewer_headers).json()
    assert (viewer["role"], viewer["can_write"]) == ("viewer", False)
    assert client.get("/api/v1/auth/session").status_code == 401


# --- payments --------------------------------------------------------------------------------


def test_payment_search_filters_sorting_and_currency_totals(
    client, owner_headers, viewer_headers
) -> None:  # type: ignore[no-untyped-def]
    pay(
        client,
        owner_headers,
        "openai",
        amount="50.00",
        payment_reference="INV-A",
        notes="card top-up",
    )
    pay(client, owner_headers, "openai", amount="10.00", kind="refund", payment_reference="RF-1")
    pay(
        client,
        owner_headers,
        "sarvam",
        amount="2000.00",
        currency="INR",
        payment_reference="UPI-77",
        transaction_date=(NOW - timedelta(days=40)).isoformat(),
    )
    url = "/api/v1/payments"
    data = client.get(url, headers=viewer_headers).json()
    assert data["total"] == 3
    assert {t["currency"]: t["net_amount"] for t in data["totals"]} == {"INR": "2000", "USD": "40"}

    assert client.get(url, params={"q": "upi"}, headers=viewer_headers).json()["total"] == 1
    assert client.get(url, params={"q": "Sarvam"}, headers=viewer_headers).json()["total"] == 1
    assert client.get(url, params={"currency": "usd"}, headers=viewer_headers).json()["total"] == 2
    assert (
        client.get(url, params={"provider": "sarvam"}, headers=viewer_headers).json()["total"] == 1
    )
    recent = client.get(
        url, params={"start": (NOW - timedelta(days=7)).isoformat()}, headers=viewer_headers
    ).json()
    assert recent["total"] == 2
    by_amount = client.get(
        url, params={"sort": "amount", "order": "asc"}, headers=viewer_headers
    ).json()
    assert [p["amount"] for p in by_amount["items"]] == ["10", "50", "2000"]
    page = client.get(url, params={"limit": 1, "offset": 1}, headers=viewer_headers).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    assert client.get(url, params={"sort": "nope"}, headers=viewer_headers).status_code == 422


def test_edit_manual_payment_with_audit(client, owner_headers, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    p = pay(client, owner_headers, payment_reference="INV-1")
    body = {
        "amount": "55.00",
        "currency": "USD",
        "transaction_date": p["transaction_date"],
        "kind": "top_up",
        "payment_reference": "INV-1b",
        "notes": "corrected",
    }
    assert (
        client.put(f"/api/v1/payments/{p['id']}", json=body, headers=viewer_headers).status_code
        == 403
    )
    resp = client.put(f"/api/v1/payments/{p['id']}", json=body, headers=owner_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == "55" and resp.json()["editable"] is True
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "payment.update")).one()
    assert audit.changes["before"]["amount"] == "50"  # type: ignore[index]
    assert audit.changes["after"]["amount"] == "55"  # type: ignore[index]
    bad = client.put(
        f"/api/v1/payments/{p['id']}", json={**body, "amount": "-1"}, headers=owner_headers
    )
    assert bad.status_code == 422


def test_provider_verified_payments_are_read_only(client, owner_headers, session) -> None:  # type: ignore[no-untyped-def]
    dg = provider_by_slug(session, "deepgram")
    session.add(
        PaymentTransaction(
            provider_id=dg.id,
            kind=PaymentKind.OTHER,
            amount=Decimal("200"),
            currency="USD",
            transaction_date=NOW - timedelta(days=1),
            external_id="order-1",
            entry_method=EntryMethod.PROVIDER_VERIFIED,
            created_by=Actor.SYSTEM,
        )
    )
    session.commit()
    pid = session.scalars(select(PaymentTransaction.id)).one()
    item = client.get(f"/api/v1/payments/{pid}", headers=owner_headers).json()
    assert item["editable"] is False
    resp = client.delete(f"/api/v1/payments/{pid}", headers=owner_headers)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "payment_not_editable"


def test_delete_manual_payment_is_audited(client, owner_headers, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    p = pay(client, owner_headers, payment_reference="DEL-1")
    assert client.delete(f"/api/v1/payments/{p['id']}", headers=viewer_headers).status_code == 403
    assert client.delete(f"/api/v1/payments/{p['id']}", headers=owner_headers).status_code == 204
    assert client.get(f"/api/v1/payments/{p['id']}", headers=owner_headers).status_code == 404
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "payment.delete")).one()
    assert audit.changes["before"]["payment_reference"] == "DEL-1"  # type: ignore[index]
    assert client.delete(f"/api/v1/payments/{p['id']}", headers=owner_headers).status_code == 404


# --- reconciliation ----------------------------------------------------------------------------


def test_payment_alone_never_changes_balance(client, owner_headers) -> None:  # type: ignore[no-untyped-def]
    manual_balance(client, owner_headers, "openai", "20.00", "USD", NOW - timedelta(days=3))
    pay(client, owner_headers, amount="100.00")
    provider = client.get("/api/v1/providers/openai", headers=owner_headers).json()
    assert provider["latest_balance"]["amount"] == "20"


def test_reconcile_flow(client, owner_headers, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    base = manual_balance(client, owner_headers, "openai", "20.00", "USD", NOW - timedelta(days=3))
    p = pay(client, owner_headers, amount="100.00")
    preview = client.get(
        f"/api/v1/payments/{p['id']}/reconcile-preview", headers=viewer_headers
    ).json()
    assert preview["allowed"] is True and preview["base"]["snapshot_id"] == base["id"]
    assert preview["payment_effect"] == "100" and preview["resulting_balance"] == "120"

    url = f"/api/v1/payments/{p['id']}/reconcile"
    assert (
        client.post(url, json={"base_snapshot_id": base["id"]}, headers=viewer_headers).status_code
        == 403
    )
    stale = client.post(url, json={"base_snapshot_id": base["id"] + 999}, headers=owner_headers)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "balance_changed"
    done = client.post(url, json={"base_snapshot_id": base["id"]}, headers=owner_headers)
    assert done.status_code == 200, done.text
    assert done.json()["amount"] == "120" and done.json()["source"] == "manual"
    assert done.json()["details"]["reconciliation"]["payment_id"] == p["id"]

    again = client.post(url, json={"base_snapshot_id": base["id"]}, headers=owner_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "reconcile_not_allowed"
    edit = client.put(
        f"/api/v1/payments/{p['id']}",
        headers=owner_headers,
        json={"amount": "90.00", "currency": "USD", "transaction_date": p["transaction_date"]},
    )
    assert edit.status_code == 409 and edit.json()["error"]["code"] == "payment_reconciled"
    assert session.scalars(select(AuditLog).where(AuditLog.action == "payment.reconcile")).one()


def test_reconcile_refund_and_refusals(client, owner_headers) -> None:  # type: ignore[no-untyped-def]
    p = pay(client, owner_headers, amount="5.00", kind="refund")
    no_base = client.get(
        f"/api/v1/payments/{p['id']}/reconcile-preview", headers=owner_headers
    ).json()
    assert no_base["allowed"] is False and "enter the current balance" in no_base["reason"]

    manual_balance(client, owner_headers, "openai", "30.00", "USD", NOW - timedelta(days=3))
    refund = client.get(
        f"/api/v1/payments/{p['id']}/reconcile-preview", headers=owner_headers
    ).json()
    assert refund["payment_effect"] == "-5" and refund["resulting_balance"] == "25"

    # A balance observed after the payment may already include it: refuse.
    manual_balance(client, owner_headers, "openai", "40.00", "USD", NOW - timedelta(hours=1))
    late = client.get(f"/api/v1/payments/{p['id']}/reconcile-preview", headers=owner_headers).json()
    assert late["allowed"] is False and "observed after" in late["reason"]

    dg = pay(client, owner_headers, "deepgram", amount="10.00")
    api_balance = client.get(
        f"/api/v1/payments/{dg['id']}/reconcile-preview", headers=owner_headers
    ).json()
    assert api_balance["allowed"] is False and "provider API" in api_balance["reason"]


# --- headroom / overview / usage series --------------------------------------------------------


def snapshot(
    session: Session,
    slug: str,
    amount: str,
    unit: str = "USD",
    kind: BalanceKind = BalanceKind.PREPAID_BALANCE,
    source: ValueSource = ValueSource.PROVIDER_API,
    at: datetime | None = None,
) -> None:
    session.add(
        ProviderBalanceSnapshot(
            provider_id=provider_by_slug(session, slug).id,
            balance_kind=kind,
            amount=Decimal(amount),
            unit=unit,
            unit_type=UnitType.CURRENCY if len(unit) == 3 else UnitType.CREDITS,
            source=source,
            observed_at=at or utcnow(),
        )
    )
    session.commit()


def usage(
    session: Session,
    slug: str,
    metric: str,
    day: datetime,
    value: str,
    unit: str = "USD",
    dimension: str = "",
) -> None:
    currency = len(unit) == 3 and unit.isupper()
    session.add(
        ProviderUsageSnapshot(
            provider_id=provider_by_slug(session, slug).id,
            metric=metric,
            dimension=dimension,
            period_start=day,
            period_end=day + timedelta(days=1),
            cost_amount=Decimal(value) if currency else None,
            cost_currency=unit if currency else None,
            quantity=None if currency else Decimal(value),
            quantity_unit=None if currency else unit,
            source=ValueSource.BILLING_REPORT,
            observed_at=utcnow(),
            is_final=True,
        )
    )
    session.commit()


def test_threshold_headroom(client, owner_headers, session) -> None:  # type: ignore[no-untyped-def]
    client.put(
        "/api/v1/providers/deepgram/threshold",
        json={"amount": "25.00", "unit": "USD"},
        headers=owner_headers,
    )
    assert (
        client.get("/api/v1/providers/deepgram", headers=owner_headers).json()["threshold_headroom"]
        is None
    )
    snapshot(session, "deepgram", "18.40")
    head = client.get("/api/v1/providers/deepgram", headers=owner_headers).json()[
        "threshold_headroom"
    ]
    assert head == {
        "amount": "-6.6",
        "unit": "USD",
        "position": "below",
        "balance_source_type": "api_verified",
        "is_stale": False,
    }
    snapshot(session, "deepgram", "25")
    head = client.get("/api/v1/providers/deepgram", headers=owner_headers).json()[
        "threshold_headroom"
    ]
    assert head["position"] == "at" and head["amount"] == "0"


def test_overview_keeps_currencies_separate(client, owner_headers, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    client.put(
        "/api/v1/providers/deepgram/threshold",
        json={"amount": "25.00", "unit": "USD"},
        headers=owner_headers,
    )
    snapshot(session, "deepgram", "10")
    client.post("/api/v1/providers/deepgram/threshold/evaluate", headers=owner_headers)
    pay(client, owner_headers, "sarvam", amount="2000.00", currency="INR")
    pay(client, owner_headers, "openai", amount="50.00")
    aws = provider_by_slug(session, "aws")
    session.add(
        ProviderUsageSnapshot(
            provider_id=aws.id,
            metric="cost_month",
            dimension="",
            period_start=month,
            period_end=month + timedelta(days=31),
            cost_amount=Decimal("45.10"),
            cost_currency="USD",
            source=ValueSource.BILLING_REPORT,
            observed_at=utcnow(),
        )
    )
    session.commit()
    usage(session, "openai", "cost", today - timedelta(days=1), "3.50")
    usage(session, "deepgram", "cost", today - timedelta(days=1), "1.25")
    usage(session, "deepgram", "audio_hours", today - timedelta(days=1), "4.5", unit="hours")

    o = client.get("/api/v1/overview", headers=viewer_headers).json()
    assert o["counts"]["providers"] == 5 and o["counts"]["low_balance"] == 1
    assert o["counts"]["not_configured"] >= 1
    [mtd] = o["aws_month_to_date"]
    assert (mtd["metric"], mtd["unit"], mtd["value"], mtd["is_final"]) == (
        "cost_month",
        "USD",
        "45.1",
        False,
    )
    assert mtd["period_start"].startswith(month.strftime("%Y-%m-01"))
    assert {(p["currency"], p["amount"]) for p in o["recent_payments"]} == {
        ("INR", "2000"),
        ("USD", "50"),
    }
    assert {(s["provider"], s["unit"]) for s in o["spend_trend"]} == {
        ("openai", "USD"),
        ("deepgram", "USD"),
    }
    assert [s["provider"] for s in o["usage_trend"]] == ["deepgram"]
    dg = next(p for p in o["providers"] if p["id"] == "deepgram")
    assert dg["alert_state"] == "breached" and dg["headroom"]["position"] == "below"
    assert "total" not in str(o["counts"])  # no cross-currency grand total anywhere


def test_usage_series(client, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for i, svc in enumerate(["Amazon EC2", "Amazon S3", "AWS Lambda"]):
        usage(session, "aws", "cost", today - timedelta(days=1), str(i + 1), dimension=svc)
    usage(session, "aws", "cost", today - timedelta(days=1), "6")
    usage(
        session,
        "aws",
        "s3_bucket_size_gb",
        today - timedelta(days=1),
        "12.5",
        unit="GB",
        dimension="media/StandardStorage",
    )
    base = "/api/v1/providers/aws/usage-series"
    data = client.get(base, params={"days": 7}, headers=viewer_headers).json()
    [cost] = [s for s in data["series"] if s["metric"] == "cost"]
    assert cost["unit"] == "USD" and cost["points"][0]["value"] == "6"
    assert any(m["metric"] == "s3_bucket_size_gb" and m["value"] == "12.5" for m in data["latest"])
    if (today - timedelta(days=1)).month == today.month:
        assert any(
            (m["metric"], m["unit"], m["value"]) == ("cost", "USD", "6")
            for m in data["month_to_date"]
        )
    by_service = client.get(
        base, params={"metric": "cost", "by_dimension": True}, headers=viewer_headers
    ).json()
    assert {s["dimension"] for s in by_service["series"]} == {
        "Amazon EC2",
        "Amazon S3",
        "AWS Lambda",
    }
    bad = client.get(base, params={"by_dimension": True}, headers=viewer_headers)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "metric_required"


# --- SSE ---------------------------------------------------------------------------------------


def test_sse_stream_reports_changed_topics(
    tmp_path: Path, migrated_template: Path, owner_headers
) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "sse.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(
        make_settings(db, sse_check_seconds=0.05, sse_max_connection_seconds=1.0),
        start_scheduler=False,
    )

    def add_payment_later() -> None:
        time.sleep(0.3)
        s = app.state.session_factory()
        try:
            s.add(
                PaymentTransaction(
                    provider_id=provider_by_slug(s, "openai").id,
                    kind=PaymentKind.TOP_UP,
                    amount=Decimal("1"),
                    currency="USD",
                    transaction_date=datetime.now(UTC),
                    entry_method=EntryMethod.MANUAL,
                    created_by=Actor.OWNER,
                )
            )
            s.commit()
        finally:
            s.close()

    with TestClient(app) as c:
        assert c.get("/api/v1/events").status_code == 401
        worker = threading.Thread(target=add_payment_later)
        worker.start()
        with c.stream("GET", "/api/v1/events", headers=owner_headers) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())
        worker.join()
    app.state.engine.dispose()
    assert "event: ready" in body
    assert 'event: changed\ndata: {"topics": ["payments"]}' in body
    assert body.rstrip().endswith("event: reconnect\ndata: {}")


# --- UI serving --------------------------------------------------------------------------------


def test_frontend_served_with_spa_fallback_and_ui_csp(
    tmp_path: Path, migrated_template: Path
) -> None:
    db = tmp_path / "ui.db"
    shutil.copyfile(migrated_template, db)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app-123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do-not-serve")
    app = create_app(make_settings(db, frontend_dist_dir=dist), start_scheduler=False)
    with TestClient(app) as c:
        index = c.get("/")
        assert index.status_code == 200 and "id=root" in index.text
        assert "script-src 'self'" in index.headers["content-security-policy"]
        assert c.get("/payments").text == index.text  # client-side route
        asset = c.get("/assets/app-123.js")
        assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
        assert "do-not-serve" not in c.get("/..%2Fsecret.txt").text
        api_404 = c.get("/api/v1/nope")
        assert api_404.status_code == 404 and api_404.json()["error"]["code"] == "not_found"
        assert (
            c.get("/api/v1/health")
            .headers["content-security-policy"]
            .startswith("default-src 'none'")
        )
    app.state.engine.dispose()


@pytest.mark.parametrize("missing", [True])
def test_api_only_when_no_build(tmp_path: Path, migrated_template: Path, missing: bool) -> None:
    db = tmp_path / "x.db"
    shutil.copyfile(migrated_template, db)
    app = create_app(make_settings(db, frontend_dist_dir=tmp_path / "none"), start_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/").status_code == 404
    app.state.engine.dispose()


def test_docs_paths_never_fall_back_to_ui_in_production(
    tmp_path: Path, migrated_template: Path
) -> None:
    db = tmp_path / "prod.db"
    shutil.copyfile(migrated_template, db)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    app = create_app(
        make_settings(db, app_env="production", cors_origins=[], frontend_dist_dir=dist),
        start_scheduler=False,
    )
    with TestClient(app) as c:
        for path in ("/docs", "/openapi.json", "/redoc"):
            resp = c.get(path)
            assert resp.status_code == 404 and "id=root" not in resp.text, path
        assert c.get("/thresholds").status_code == 200  # real UI routes still work
    app.state.engine.dispose()


def test_overview_month_to_date_spend_per_provider(client, viewer_headers, session) -> None:  # type: ignore[no-untyped-def]
    """Provider cards show this month's provider-reported spend: provider totals only (not
    per-model rows), this month only, one entry per currency, summed on the server."""
    month = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage(session, "openai", "cost", month, "10.25")
    usage(session, "openai", "cost", month + timedelta(days=1), "4.75")
    usage(session, "openai", "cost", month, "99.00", dimension="gpt-4o")  # breakdown row
    usage(session, "openai", "cost", month - timedelta(days=1), "500.00")  # last month
    usage(session, "openai", "input_tokens", month, "1000", unit="tokens")  # not money
    usage(session, "elevenlabs", "cost", month, "80.00", unit="INR")
    usage(session, "elevenlabs", "credits_used", month, "1200", unit="CREDITS")
    usage(session, "elevenlabs", "credits_used", month + timedelta(days=1), "300", unit="CREDITS")
    usage(session, "elevenlabs", "credits_used", month - timedelta(days=1), "9999", unit="CREDITS")
    usage(session, "elevenlabs", "cost", month + timedelta(days=1), "1.50", unit="USD")

    rows = {
        p["id"]: p
        for p in client.get("/api/v1/overview", headers=viewer_headers).json()["providers"]
    }
    assert [(m["unit"], m["value"]) for m in rows["openai"]["month_to_date_cost"]] == [
        ("USD", "15")
    ]
    assert [(m["unit"], m["value"]) for m in rows["elevenlabs"]["month_to_date_cost"]] == [
        ("INR", "80"),
        ("USD", "1.5"),
    ]
    assert rows["sarvam"]["month_to_date_cost"] == []
    assert [
        (m["metric"], m["unit"], m["value"]) for m in rows["elevenlabs"]["month_to_date_usage"]
    ] == [("credits_used", "CREDITS", "1500")]
    assert rows["openai"]["balance_capability"] == "manual_only"
    assert rows["openai"]["spend_reported"] is True
    assert rows["sarvam"]["spend_reported"] is False  # no usage or billing API
    assert rows["sarvam"]["month_to_date_usage"] == []
    assert rows["deepgram"]["balance_capability"] == "api"
