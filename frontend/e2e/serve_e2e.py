"""E2E test server: a throwaway database with FIXTURE data + the real backend and UI.

Run with the backend's virtualenv Python (Playwright starts it automatically):
    ../backend/venv/Scripts/python e2e/serve_e2e.py

* Uses a temporary SQLite file, never backend/data/monitor.db.
* POLLING_ENABLED=false: no provider, status page or AWS API is ever contacted.
* Provider keys are obvious dummies so integrations display as configured.
Everything seeded here is test fixture data for screenshots and browser tests only.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1]
BACKEND = FRONTEND.parent / "backend"
sys.path.insert(0, str(BACKEND))

OWNER = "e2e-owner-token-not-secret-0123456789abcdef"
VIEWER = "e2e-viewer-token-not-secret-0123456789abcdef"
PORT = int(os.environ.get("E2E_PORT", "8187"))

db_path = Path(tempfile.mkdtemp(prefix="acm-e2e-")) / "e2e.db"
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "DASHBOARD_OWNER_TOKEN": OWNER,
        "DASHBOARD_VIEWER_TOKEN": VIEWER,
        "POLLING_ENABLED": "false",
        "APP_ENV": "development",
        "LOG_LEVEL": "WARNING",
        "FRONTEND_DIST_DIR": str(FRONTEND / "dist"),
        "OPENAI_ADMIN_KEY": "e2e-dummy-not-a-real-key-000000",
        "DEEPGRAM_API_KEY": "e2e-dummy-not-a-real-key-000000",
        "DEEPGRAM_PROJECT_ID": "e2e-project",
        "ELEVENLABS_API_KEY": "e2e-dummy-not-a-real-key-000000",
        "AWS_MONITORING_ENABLED": "true",
        "AWS_ACCESS_KEY_ID": "AKIAE2EDUMMYNOTREAL0",
        "AWS_SECRET_ACCESS_KEY": "e2e-dummy-not-a-real-secret",
        "EMAIL_DELIVERY_ENABLED": "false",
        "DEFAULT_DISPLAY_TIMEZONE": "Asia/Kolkata",
        "SSE_CHECK_SECONDS": "1",
        # Never reach the real AdhyaapakAI backend with the real admin secret from backend/.env.
        # ...and never read the real app database (ADHYAAPAK_MONGO_URL in backend/.env).
        "ADHYAAPAK_MONGO_URL": "",
        "ADHYAAPAK_MONGO_DB": "",
        "ADHYAAPAK_JWT_SECRET": "",
    }
)

from app.db.migrations import upgrade_to_head  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.models import (  # noqa: E402
    AlertDeliveryAttempt,
    AlertEvent,
    InfrastructureResourceSnapshot,
    PaymentTransaction,
    Provider,
    ProviderBalanceSnapshot,
    ProviderSyncState,
    ProviderUsageSnapshot,
    ServiceHealthCheck,
    ThresholdConfig,
)
from app.models.enums import (  # noqa: E402
    Actor,
    AlertEventType,
    AlertState,
    AttemptOutcome,
    BalanceKind,
    DeliveryStatus,
    EntryMethod,
    EvaluationStatus,
    HealthSource,
    HealthStatus,
    PaymentKind,
    UnitType,
    ValueSource,
)

NOW = datetime.now(UTC).replace(microsecond=0)
TODAY = NOW.replace(hour=0, minute=0, second=0)
D = Decimal


def seed() -> None:
    url = os.environ["DATABASE_URL"]
    upgrade_to_head(url)
    engine = create_db_engine(url)
    s = create_session_factory(engine)()
    p = {row.slug: row for row in s.query(Provider)}

    def bal(slug, amount, unit, kind, source, age_min, details=None):  # type: ignore[no-untyped-def]
        s.add(ProviderBalanceSnapshot(
            provider_id=p[slug].id, balance_kind=kind, amount=D(amount), unit=unit,
            unit_type=UnitType.CURRENCY if len(unit) == 3 else UnitType.CREDITS, source=source,
            observed_at=NOW - timedelta(minutes=age_min), details=details))

    # Balances (history + latest)
    for i, amt in enumerate(["61.20", "48.90", "35.10", "27.80", "18.40"]):
        bal("deepgram", amt, "USD", BalanceKind.PREPAID_BALANCE, ValueSource.PROVIDER_API, (4 - i) * 1440 + 5)
    bal("openai", "40.00", "USD", BalanceKind.PREPAID_BALANCE, ValueSource.MANUAL, 3 * 1440)
    bal("openai", "31.75", "USD", BalanceKind.PREPAID_BALANCE, ValueSource.USAGE_ESTIMATE, 20)
    bal("sarvam", "2500.00", "INR", BalanceKind.PREPAID_BALANCE, ValueSource.MANUAL, 4 * 1440)
    bal("elevenlabs", "41250", "CREDITS", BalanceKind.PLAN_QUOTA, ValueSource.PROVIDER_API, 12,
        {"credit_limit": 100000, "credits_used": 58750, "resets_at": (TODAY + timedelta(days=17)).isoformat(),
         "tier": "creator"})
    bal("aws", "212.40", "USD", BalanceKind.PROMOTIONAL_CREDIT, ValueSource.BILLING_REPORT, 300)

    # Daily usage/cost for 30 days
    for day in range(30):
        start = TODAY - timedelta(days=29 - day)
        final = day < 28

        def use(slug, metric, value, unit, dimension=""):  # type: ignore[no-untyped-def]
            money = len(unit) == 3 and unit.isupper()
            s.add(ProviderUsageSnapshot(
                provider_id=p[slug].id, metric=metric, dimension=dimension, period_start=start,
                period_end=start + timedelta(days=1), quantity=None if money else D(value),
                quantity_unit=None if money else unit, cost_amount=D(value) if money else None,
                cost_currency=unit if money else None,
                source=ValueSource.BILLING_REPORT if metric == "cost" else ValueSource.PROVIDER_API,
                is_final=final, observed_at=NOW - timedelta(minutes=15)))

        wave = (day % 7) / 10
        use("openai", "cost", f"{1.8 + wave:.2f}", "USD")
        use("openai", "requests", str(900 + day * 21), "requests")
        use("deepgram", "cost", f"{1.1 + wave * 1.5:.2f}", "USD")
        use("deepgram", "audio_hours", f"{3.2 + wave * 2:.2f}", "hours")
        use("elevenlabs", "credits_used", str(1500 + (day % 5) * 220), "CREDITS")
        for svc, base in (("Amazon EC2", 2.4), ("Amazon S3", 0.42), ("AWS Lambda", 0.08)):
            use("aws", "cost", f"{base + wave:.2f}", "USD", svc)
        use("aws", "cost", f"{2.9 + wave * 3:.2f}", "USD")

    month = TODAY.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    for metric, value, src in (("cost_month", "41.37", ValueSource.BILLING_REPORT), ("cost_forecast_month", "98.10", ValueSource.USAGE_ESTIMATE)):
        s.add(ProviderUsageSnapshot(provider_id=p["aws"].id, metric=metric, period_start=month, period_end=next_month,
                                    cost_amount=D(value), cost_currency="USD", source=src, observed_at=NOW - timedelta(hours=5)))
    s.add(ProviderUsageSnapshot(provider_id=p["aws"].id, metric="s3_bucket_size_gb", dimension="adhyaapak-media/StandardStorage",
                                period_start=TODAY - timedelta(days=1), period_end=TODAY, quantity=D("184.25"), quantity_unit="GB",
                                source=ValueSource.BILLING_REPORT, is_final=True, observed_at=NOW))

    # Payments
    pays = [
        ("openai", "50.00", "USD", 9, "OAI-2026-0091", EntryMethod.MANUAL, PaymentKind.TOP_UP, "Card top-up"),
        ("sarvam", "2000.00", "INR", 12, "UPI-77821", EntryMethod.MANUAL, PaymentKind.TOP_UP, "UPI"),
        ("deepgram", "200.00", "USD", 20, None, EntryMethod.PROVIDER_VERIFIED, PaymentKind.OTHER, "Deepgram order"),
        ("elevenlabs", "22.00", "USD", 25, "EL-INV-3381", EntryMethod.MANUAL, PaymentKind.SUBSCRIPTION, "Creator plan"),
        ("openai", "5.00", "USD", 30, "OAI-RF-12", EntryMethod.MANUAL, PaymentKind.REFUND, "Partial refund"),
    ]
    for i, (slug, amt, cur, days, ref, method, kind, note) in enumerate(pays):
        s.add(PaymentTransaction(provider_id=p[slug].id, kind=kind, amount=D(amt), currency=cur,
                                 transaction_date=NOW - timedelta(days=days), payment_reference=ref,
                                 external_id=f"order-{i}" if method is EntryMethod.PROVIDER_VERIFIED else None,
                                 entry_method=method, notes=note,
                                 created_by=Actor.SYSTEM if method is EntryMethod.PROVIDER_VERIFIED else Actor.OWNER))

    # Thresholds
    def thr(slug, amount, unit, kind, state, status, msg, **kw):  # type: ignore[no-untyped-def]
        t = ThresholdConfig(provider_id=p[slug].id, balance_kind=kind, amount=D(amount), unit=unit,
                            unit_type=UnitType.CURRENCY if len(unit) == 3 else UnitType.CREDITS,
                            alert_state=state, last_evaluation_status=status, last_evaluation_message=msg,
                            last_evaluated_at=NOW, recovery_margin=D(kw.pop("margin", "5")), **kw)
        s.add(t)
        return t

    t_dg = thr("deepgram", "25.00", "USD", BalanceKind.PREPAID_BALANCE, AlertState.BREACHED, EvaluationStatus.BREACHED,
               "18.40 USD <= threshold 25.00 USD", breach_episode=1, cooldown_minutes=360)
    thr("openai", "20.00", "USD", BalanceKind.PREPAID_BALANCE, AlertState.OK, EvaluationStatus.OK,
        "31.75 USD above recovery boundary", eligible_sources=["manual", "usage_estimate"])
    thr("sarvam", "500.00", "INR", BalanceKind.PREPAID_BALANCE, AlertState.UNKNOWN, EvaluationStatus.STALE,
        "latest eligible balance is 5760 min old (limit 1440 min); not acting on stale data", eligible_sources=["manual"])
    thr("elevenlabs", "10000", "CREDITS", BalanceKind.PLAN_QUOTA, AlertState.OK, EvaluationStatus.OK,
        "41,250 credits above recovery boundary", margin="0")
    s.flush()

    # Alerts
    def alert(event_type, status, minutes, attempts, error=None, key="", observed="18.40"):  # type: ignore[no-untyped-def]
        a = AlertEvent(provider_id=p["deepgram"].id, threshold_config_id=t_dg.id, event_type=event_type,
                       observed_amount=D(observed), threshold_amount=D("25.00"), unit="USD", recipients=["ops@example.com"],
                       delivery_status=status, delivery_attempts=attempts, last_error=error, dedupe_key=key,
                       episode=1, subject=f"[Low balance] Deepgram at {observed} USD (threshold 25.00 USD)",
                       body_text="Fixture alert body", body_html="<p>Fixture alert email (e2e test data)</p>",
                       context={"source_type": "api_verified"}, occurred_at=NOW - timedelta(minutes=minutes),
                       sent_at=NOW - timedelta(minutes=minutes - 1) if status is DeliveryStatus.SENT else None)
        s.add(a)
        s.flush()
        for n in range(1, attempts + 1):
            ok = status is DeliveryStatus.SENT and n == attempts
            s.add(AlertDeliveryAttempt(alert_event_id=a.id, attempt_number=n,
                                       outcome=AttemptOutcome.SENT if ok else AttemptOutcome.FAILED if status is DeliveryStatus.FAILED and n == attempts else AttemptOutcome.RETRY,
                                       error_code=None if ok else "unavailable", error_message=None if ok else "SMTP server disconnected",
                                       started_at=NOW - timedelta(minutes=minutes - n), finished_at=NOW - timedelta(minutes=minutes - n)))

    alert(AlertEventType.BREACHED, DeliveryStatus.SENT, 300, 2, key="e2e:1")
    alert(AlertEventType.REMINDER, DeliveryStatus.FAILED, 60, 6, "unavailable: SMTP server disconnected", key="e2e:2", observed="18.40")
    alert(AlertEventType.REMINDER, DeliveryStatus.SUPPRESSED, 10, 0, "email delivery disabled (EMAIL_DELIVERY_ENABLED=false)", key="e2e:3")

    # Sync states and health
    for slug in ("deepgram", "openai", "elevenlabs", "aws", "sarvam"):
        for op in ("balance", "usage", "billing", "health"):
            failing = slug == "elevenlabs" and op == "usage"
            unsupported = slug == "sarvam" and op != "health"
            s.add(ProviderSyncState(provider_id=p[slug].id, operation=op,
                                    status="unsupported" if unsupported else "error" if failing else "ok",
                                    last_attempt_at=NOW - timedelta(minutes=10),
                                    last_success_at=None if unsupported else NOW - timedelta(hours=7 if failing else 0, minutes=10),
                                    consecutive_failures=3 if failing else 0,
                                    last_error_code="rate_limited" if failing else None,
                                    last_error_message="rate limited by provider (429)" if failing else None))
        s.add(ServiceHealthCheck(provider_id=p[slug].id, source=HealthSource.API_PROBE if slug == "aws" else HealthSource.STATUS_PAGE,
                                 status=HealthStatus.DEGRADED if slug == "aws" else HealthStatus.OPERATIONAL,
                                 indicator="impaired" if slug == "aws" else "none",
                                 description="credentials valid; 3 instance(s) checked; 1 impaired" if slug == "aws" else "All Systems Operational",
                                 latency_ms=180, http_status=200, checked_at=NOW - timedelta(minutes=4)))

    # AWS resources
    for rid, name, state, health, itype in (("i-0a1b2c3d4e5f60001", "api-server", "running", "ok", "t3.medium"),
                                            ("i-0a1b2c3d4e5f60002", "tutor-worker", "running", "impaired", "t3.large"),
                                            ("i-0a1b2c3d4e5f60003", "staging", "stopped", None, "t3.small")):
        s.add(InfrastructureResourceSnapshot(provider_id=p["aws"].id, resource_type="ec2_instance", resource_id=rid,
                                             region="ap-south-1", name=name, state=state, health_status=health,
                                             details={"instance_type": itype, "availability_zone": "ap-south-1a",
                                                      "instance_status": health, "system_status": "ok" if health else None,
                                                      "scheduled_events": 0}, observed_at=NOW - timedelta(minutes=8)))
    for vid, size, vtype, attached in (("vol-0e2e0001", 30, "gp3", ["i-0a1b2c3d4e5f60001"]), ("vol-0e2e0002", 100, "gp3", ["i-0a1b2c3d4e5f60002"]), ("vol-0e2e0003", 8, "gp2", [])):
        s.add(InfrastructureResourceSnapshot(provider_id=p["aws"].id, resource_type="ebs_volume", resource_id=vid,
                                             region="ap-south-1", name=None, state="in-use" if attached else "available",
                                             details={"size_gib": size, "volume_type": vtype, "encrypted": bool(attached),
                                                      "availability_zone": "ap-south-1a", "attached_instances": attached},
                                             observed_at=NOW - timedelta(minutes=8)))

    s.commit()
    s.close()
    engine.dispose()


if __name__ == "__main__":
    seed()
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=PORT, log_level="warning",
                app_dir=str(BACKEND))
