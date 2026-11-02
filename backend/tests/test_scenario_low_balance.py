"""Phase 6 operational scenario: a provider balance falls from 1,000 INR through a 300 INR
threshold, recovers, then the provider fails and the application restarts.

The example provider is the INR-denominated Sarvam slot, fed by a scripted test adapter
(Sarvam has no balance API; in production its balance is entered manually). Everything
downstream is production code: the polling coordinator, ingest, threshold evaluation, the
transactional outbox, the real SMTP protocol to a local sink (no real recipient), the HTTP
API with authentication, the scheduler's alert cycle and a restart on the same database
file. No provider is contacted and no credits are spent.

Time is simulated only by moving stored timestamps back (cooldown and staleness windows),
which is what elapsed wall-clock time would do.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from decimal import Decimal as D

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.models import AlertEvent, PaymentTransaction, ProviderBalanceSnapshot, ThresholdConfig
from app.providers.base import CapabilityInfo, ProviderAdapter
from app.providers.registry import ProviderRuntime, build_runtimes
from app.providers.types import (
    ErrorCode,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    ProviderError,
    ProviderOperationError,
    SourceType,
    SyncStatus,
)
from app.services.polling import PollingScheduler
from tests.smtp_sink import SmtpSink
from tests.test_phase3_api import mail_settings

P = "/api/v1/providers/sarvam"
THRESHOLD = {"amount": "300.00", "unit": "INR", "recovery_margin": "50.00"}  # cooldown: none


class ScriptedInrBalance(ProviderAdapter):
    """Test-only balance feed: returns whatever the scenario sets, or fails like an outage."""

    slug = "sarvam"
    capabilities = {Operation.BALANCE: CapabilityInfo(True, "scripted test feed")}

    def __init__(self) -> None:
        self.amount = Decimal(0)
        self.failing = False

    async def _balance(self) -> OperationResult:
        if self.failing:
            raise ProviderOperationError(
                ProviderError(ErrorCode.UPSTREAM_ERROR, "provider returned HTTP 503", True, 503)
            )
        now = utcnow()
        metric = Metric(
            self.slug, MetricType.PREPAID_BALANCE, self.amount, "INR", SourceType.API_VERIFIED, now
        )
        return self.result(Operation.BALANCE, observed_at=now, metrics=(metric,))


def build(settings: Settings, feed: ScriptedInrBalance) -> FastAPI:
    runtimes = build_runtimes(settings)
    runtimes["sarvam"] = ProviderRuntime(
        "sarvam", feed, SyncStatus.OK, capabilities=dict(ScriptedInrBalance.capabilities)
    )
    return create_app(settings, runtimes=runtimes, start_scheduler=False)


def balance(feed: ScriptedInrBalance, amount: str) -> None:
    feed.amount, feed.failing = Decimal(amount), False


class Scenario:
    def __init__(self, app: FastAPI, client: TestClient, headers: dict[str, str]) -> None:
        self.app, self.c, self.h = app, client, headers
        self.trace: list[str] = []

    def sync(self) -> dict:
        self.app.state.coordinator._last_manual.clear()  # manual-sync cooldown not under test
        resp = self.c.post(f"{P}/sync", headers=self.h)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def monitoring_cycle(self) -> None:
        """Exactly what the background scheduler runs each tick (evaluate all + send)."""
        scheduler = PollingScheduler(
            self.app.state.coordinator, dispatcher=self.app.state.dispatcher
        )
        asyncio.run(scheduler._alert_cycle())

    def threshold(self) -> dict:
        return self.c.get(f"{P}/threshold", headers=self.h).json()

    def alerts(self) -> list[dict]:
        body = self.c.get("/api/v1/alerts", params={"provider": "sarvam"}, headers=self.h).json()
        return sorted(body["items"], key=lambda a: a["id"])

    def provider(self) -> dict:
        return self.c.get(P, headers=self.h).json()

    def rewind(self, model, column: str, delta: timedelta) -> None:  # type: ignore[no-untyped-def]
        """Simulate elapsed time: move a stored timestamp back by `delta`."""
        with self.app.state.session_factory() as s, s.begin():
            for row in s.scalars(select(model)):
                if getattr(row, column) is not None:
                    setattr(row, column, getattr(row, column) - delta)

    def scalar(self, stmt):  # type: ignore[no-untyped-def]
        with self.app.state.session_factory() as s:
            return s.scalar(stmt)

    def log(self, step: str, **observed: object) -> None:
        self.trace.append(f"{step}: " + ", ".join(f"{k}={v}" for k, v in observed.items()))


def kinds(alerts: list[dict]) -> list[str]:
    return [a["event_type"] for a in alerts]


def test_low_balance_lifecycle_and_restart(settings, owner_headers) -> None:  # type: ignore[no-untyped-def]
    with SmtpSink() as sink:
        cfg = mail_settings(settings, sink)
        fake = ScriptedInrBalance()
        app = build(cfg, fake)
        with TestClient(app) as client:
            s = Scenario(app, client, owner_headers)

            # 1. Provider reports 1,000 INR.
            balance(fake, "1000.00")
            s.sync()
            latest = s.provider()["latest_balance"]
            assert (D(latest["amount"]), latest["unit"], latest["source"]) == (
                1000,
                "INR",
                "provider_api",
            )
            s.log("1 balance", amount=latest["amount"], unit=latest["unit"])

            # 2. Threshold 300 INR (recover above 350; one email per breach).
            resp = client.put(f"{P}/threshold", json=THRESHOLD, headers=owner_headers)
            assert resp.status_code == 200, resp.text

            # 3. No alert at 1,000.
            s.monitoring_cycle()
            t = s.threshold()
            assert t["alert_state"] == "ok" and s.alerts() == [] and sink.messages == []
            s.log("3 at 1000", state=t["alert_state"], alerts=0, emails=0)

            # 4-5. Falls to 500: still above the threshold, no alert.
            balance(fake, "500.00")
            s.sync()
            s.monitoring_cycle()
            t = s.threshold()
            headroom = s.provider()["threshold_headroom"]
            assert t["alert_state"] == "ok" and s.alerts() == [] and sink.messages == []
            assert (D(headroom["amount"]), headroom["position"]) == (200, "above")
            s.log("5 at 500", state=t["alert_state"], headroom=headroom["amount"], alerts=0)

            # 6-7. Falls to exactly 300 (at the threshold counts as low): one event, queued.
            balance(fake, "300.00")
            s.sync()
            [alert] = s.alerts()
            assert (alert["event_type"], alert["delivery_status"]) == ("breached", "pending")
            assert (D(alert["observed_amount"]), D(alert["threshold_amount"])) == (300, 300)
            assert sink.messages == []  # queued in the outbox, not sent inline
            s.monitoring_cycle()  # the scheduler tick delivers the outbox
            assert [a["delivery_status"] for a in s.alerts()] == ["sent"]
            [mail] = sink.messages
            assert mail.rcpt_to == ["ops@example.com"]
            assert "Sarvam AI at 300.00 INR (threshold 300.00 INR)" in mail.message["Subject"]
            s.log(
                "7 at 300", state=s.threshold()["alert_state"], events=kinds(s.alerts()), emails=1
            )

            # 8. Repeated monitoring cycles and a manual re-evaluation: suppressed.
            for _ in range(3):
                s.sync()
                s.monitoring_cycle()
            assert client.post(f"{P}/threshold/evaluate", headers=owner_headers).status_code == 200
            s.monitoring_cycle()
            assert kinds(s.alerts()) == ["breached"] and len(sink.messages) == 1
            s.log("8 repeat x4", events=len(s.alerts()), emails=len(sink.messages))

            # 9a. Falls to 200 with the default policy (no cooldown): no second email.
            balance(fake, "200.00")
            s.sync()
            s.monitoring_cycle()
            t = s.threshold()
            assert t["alert_state"] == "breached" and t["last_evaluation_status"] == "breached"
            assert kinds(s.alerts()) == ["breached"] and len(sink.messages) == 1
            s.log("9a at 200, no cooldown", state=t["alert_state"], events=1, emails=1)

            # 9b. Policy changed to a 60-minute reminder. Not due yet (breach email just sent).
            resp = client.put(
                f"{P}/threshold", json={**THRESHOLD, "cooldown_minutes": 60}, headers=owner_headers
            )
            assert resp.status_code == 200 and resp.json()["alert_state"] == "breached"  # not reset
            s.monitoring_cycle()
            assert kinds(s.alerts()) == ["breached"]
            # 61 minutes pass: exactly one reminder, then the cooldown restarts.
            s.rewind(ThresholdConfig, "last_notified_at", timedelta(minutes=61))
            s.monitoring_cycle()
            s.monitoring_cycle()
            assert kinds(s.alerts()) == ["breached", "reminder"]
            assert [a["delivery_status"] for a in s.alerts()] == ["sent", "sent"]
            assert len(sink.messages) == 2 and "200.00 INR" in sink.messages[1].message["Subject"]
            s.log("9b cooldown 60m elapsed", events=kinds(s.alerts()), emails=len(sink.messages))

            # A recorded payment is not proof the balance increased: no automatic change.
            pay = client.post(
                f"{P}/payments",
                json={
                    "currency": "INR",
                    "amount": "2000.00",
                    "transaction_date": "2026-10-15T09:00:00+05:30",
                    "payment_reference": "SCENARIO-TOPUP-1",
                },
                headers=owner_headers,
            )
            assert pay.status_code == 201, pay.text
            s.monitoring_cycle()
            assert D(s.provider()["latest_balance"]["amount"]) == 200
            assert s.threshold()["alert_state"] == "breached"

            # 10a. 320 INR is above the threshold but inside the 50 INR margin: still breached.
            balance(fake, "320.00")
            s.sync()
            s.monitoring_cycle()
            assert s.threshold()["alert_state"] == "breached"
            assert kinds(s.alerts()) == ["breached", "reminder"]
            # 10b. Provider now reports 1,200 INR (> 350): one recovery email, state ok.
            balance(fake, "1200.00")
            s.sync()
            s.monitoring_cycle()
            s.sync()
            s.monitoring_cycle()
            t = s.threshold()
            assert t["alert_state"] == "ok"
            assert kinds(s.alerts()) == ["breached", "reminder", "recovered"]
            assert len(sink.messages) == 3
            s.log("10 recovery", state=t["alert_state"], events=kinds(s.alerts()), emails=3)

            # 11. Provider fails: last good balance kept (never zero), sync marked failed.
            snapshots_before = s.scalar(select(func.count()).select_from(ProviderBalanceSnapshot))
            fake.failing = True
            summary = s.sync()
            assert any(op["status"] == "error" for op in summary["operations"]), summary
            p = s.provider()
            assert D(p["latest_balance"]["amount"]) == 1200
            assert (
                s.scalar(select(func.count()).select_from(ProviderBalanceSnapshot))
                == snapshots_before
            )
            balance_state = next(
                st
                for st in client.get(f"{P}/sync-status", headers=owner_headers).json()["operations"]
                if st["operation"] == "balance"
            )
            assert balance_state["status"] == "error" and balance_state["consecutive_failures"] == 1
            # The stale window passes with the provider still failing.
            stale_after = p["stale_after_minutes"]
            s.rewind(ProviderBalanceSnapshot, "observed_at", timedelta(minutes=stale_after + 5))
            s.sync()
            s.monitoring_cycle()
            p = s.provider()
            t = s.threshold()
            assert D(p["latest_balance"]["amount"]) == 1200
            assert p["latest_balance_freshness"]["is_stale"] is True
            assert p["threshold_headroom"]["is_stale"] is True
            overview = client.get("/api/v1/overview", headers=owner_headers).json()
            card = next(x for x in overview["providers"] if x["id"] == "sarvam")
            assert overview["counts"]["stale_balances"] >= 1
            assert card["balance_is_stale"] is True and "balance" in card["failing_operations"]
            assert t["last_evaluation_status"] == "stale" and t["alert_state"] == "ok"
            assert len(s.alerts()) == 3 and len(sink.messages) == 3  # stale data never alerts
            s.log(
                "11 provider failure",
                balance=p["latest_balance"]["amount"],
                stale=True,
                evaluation=t["last_evaluation_status"],
            )
            before_restart = {
                "alerts": [(a["id"], a["event_type"], a["delivery_status"]) for a in s.alerts()],
                "threshold": {
                    k: t[k] for k in ("alert_state", "amount", "unit", "cooldown_minutes")
                },
                "snapshots": s.scalar(select(func.count()).select_from(ProviderBalanceSnapshot)),
                "payments": s.scalar(select(func.count()).select_from(PaymentTransaction)),
            }
        app.state.engine.dispose()

        # 12. Restart: a new process on the same database file.
        app2 = build(cfg, fake)
        with TestClient(app2) as client2:
            r = Scenario(app2, client2, owner_headers)
            t = r.threshold()
            assert {k: t[k] for k in before_restart["threshold"]} == before_restart["threshold"]
            assert [
                (a["id"], a["event_type"], a["delivery_status"]) for a in r.alerts()
            ] == before_restart["alerts"]
            assert (
                r.scalar(select(func.count()).select_from(ProviderBalanceSnapshot))
                == before_restart["snapshots"]
            )
            assert (
                r.scalar(select(func.count()).select_from(PaymentTransaction))
                == before_restart["payments"]
            )
            manual = client2.get(
                "/api/v1/payments", params={"q": "SCENARIO-TOPUP-1"}, headers=owner_headers
            ).json()["items"]
            assert [(D(m["amount"]), m["currency"]) for m in manual] == [(2000, "INR")]
            p = r.provider()
            assert D(p["latest_balance"]["amount"]) == 1200
            assert p["latest_balance_freshness"]["is_stale"] is True
            assert r.scalar(select(ThresholdConfig.breach_episode)) == 1
            # Nothing is re-sent or duplicated after the restart.
            r.monitoring_cycle()
            r.sync()
            r.monitoring_cycle()
            assert len(r.alerts()) == 3 and len(sink.messages) == 3
            assert r.scalar(select(func.count()).select_from(AlertEvent)) == 3
            ready = client2.get("/api/v1/ready").json()
            assert ready["checks"]["database"] == "ok" and ready["checks"]["migrations"] == "ok"
            r.log(
                "12 restart",
                state=t["alert_state"],
                alerts=3,
                emails=3,
                payments=before_restart["payments"],
            )
        app2.state.engine.dispose()

    assert Decimal(before_restart["threshold"]["amount"]) == Decimal(300)
    print("\n".join(["", "SCENARIO TRACE", *s.trace, *r.trace]))
