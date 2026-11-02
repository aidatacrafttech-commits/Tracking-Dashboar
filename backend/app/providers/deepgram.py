"""Deepgram adapter (Management API, ``Authorization: Token <key>``).

Verified against developers.deepgram.com reference pages:
  GET /v1/projects/{id}/balances          -> {"balances":[{balance_id, amount, units,
                                              purchase_order_id}]}
  GET /v1/projects/{id}/usage/breakdown   start/end YYYY-MM-DD -> results[{hours, total_hours,
      agent_hours, tokens_in, tokens_out, tts_characters, requests, grouping{start,end,...}}]
  GET /v1/projects/{id}/billing/breakdown start/end -> results[{dollars, grouping{start,end,...}}]
  GET /v1/projects/{id}/purchases         limit -> {"orders":[{order_id, expiration, created,
      amount, units, order_type}]}
Balance needs the billing:read scope (owner/admin key); usage needs usage:read.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from app.core.time import utcnow
from app.models.enums import PaymentKind
from app.providers.base import CapabilityInfo, ProviderAdapter, require, require_list, to_decimal
from app.providers.common import is_closed_period, parse_date, parse_iso_datetime
from app.providers.http import ResilientHttpClient, Sleep
from app.providers.statuspage import fetch_statuspage_health
from app.providers.types import (
    DataLatency,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    ProviderPayment,
    SourceType,
)

API_BASE = "https://api.deepgram.com"
STATUS_BASE = "https://status.deepgram.com"
SETTLE = timedelta(days=2)
PURCHASE_LIMIT = 100


class DeepgramAdapter(ProviderAdapter):
    slug = "deepgram"
    capabilities = {
        Operation.BALANCE: CapabilityInfo(True, "Project balances API (actual USD balance)"),
        Operation.USAGE: CapabilityInfo(
            True, "Usage breakdown API: daily audio hours, requests, TTS characters, tokens"
        ),
        Operation.BILLING: CapabilityInfo(
            True, "Billing breakdown API (daily USD) and purchases (orders)"
        ),
        Operation.HEALTH: CapabilityInfo(True, "status.deepgram.com Statuspage JSON"),
    }

    def __init__(
        self,
        api_key: str,
        project_id: str,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.project_path = f"/v1/projects/{project_id}"
        self.api = ResilientHttpClient(
            self.slug,
            API_BASE,
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout,
            max_retries=max_retries,
            transport=transport,
            sleep=sleep,
        )
        self.status = ResilientHttpClient(
            self.slug, STATUS_BASE, timeout=timeout, max_retries=1, transport=transport, sleep=sleep
        )

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.status.aclose()

    @staticmethod
    def _period(grouping: object) -> tuple[datetime, datetime]:
        start = parse_date(require(grouping, "start", "grouping.start"), "grouping.start")
        end = parse_date(require(grouping, "end", "grouping.end"), "grouping.end")
        return start, end + timedelta(days=1)  # Deepgram end dates are inclusive

    async def _balance(self) -> OperationResult:
        now = utcnow()
        payload = await self.api.get_json(f"{self.project_path}/balances")
        # Only an explicit empty list means "no balance left". A missing, null or reshaped
        # field is a malformed response, never a verified $0 (that would trigger a false
        # low-balance alert).
        balances = require_list(payload, "balances", "balances")
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for item in balances:
            units = str(require(item, "units", "balance units")).upper()
            totals[units] += to_decimal(require(item, "amount", "balance amount"), "balance amount")
        warnings: list[str] = []
        if not totals:
            totals["USD"] = Decimal(0)  # API reported no balances at all
        if len(totals) > 1:
            warnings.append(f"balances in several units ({', '.join(sorted(totals))}); USD shown")
        unit = "USD" if "USD" in totals else sorted(totals)[0]
        metric = Metric(
            self.slug,
            MetricType.PREPAID_BALANCE,
            totals[unit],
            unit,
            SourceType.API_VERIFIED,
            now,
            details={
                "balance_count": len(balances),
                "all_units": {k: str(v) for k, v in totals.items()},
            },
        )
        return self.result(
            Operation.BALANCE, observed_at=now, metrics=(metric,), warnings=tuple(warnings)
        )

    async def _usage(self, start: datetime, end: datetime) -> OperationResult:
        now = utcnow()
        payload = await self.api.get_json(
            f"{self.project_path}/usage/breakdown",
            params={"start": start.date().isoformat(), "end": end.date().isoformat()},
        )
        fields = (
            ("hours", MetricType.AUDIO_HOURS, "hours"),
            ("requests", MetricType.REQUESTS, "requests"),
            ("tts_characters", MetricType.TTS_CHARACTERS, "characters"),
            ("tokens_in", MetricType.INPUT_TOKENS, "tokens"),
            ("tokens_out", MetricType.OUTPUT_TOKENS, "tokens"),
        )
        metrics: list[Metric] = []
        for row in require_list(payload, "results", "results"):
            p_start, p_end = self._period(require(row, "grouping", "grouping"))
            final = is_closed_period(p_end, now, SETTLE)
            for key, metric_type, unit in fields:
                metrics.append(
                    Metric(
                        self.slug,
                        metric_type,
                        to_decimal(require(row, key, key), key),
                        unit,
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_end,
                        is_final=final,
                    )
                )
        return self.result(Operation.USAGE, observed_at=now, metrics=tuple(metrics))

    async def _billing(self, start: datetime, end: datetime) -> OperationResult:
        now = utcnow()
        breakdown = await self.api.get_json(
            f"{self.project_path}/billing/breakdown",
            params={"start": start.date().isoformat(), "end": end.date().isoformat()},
        )
        daily: dict[tuple[datetime, datetime], Decimal] = defaultdict(Decimal)
        for row in require_list(breakdown, "results", "results"):
            period = self._period(require(row, "grouping", "grouping"))
            daily[period] += to_decimal(require(row, "dollars", "dollars"), "dollars")
        metrics = tuple(
            Metric(
                self.slug,
                MetricType.COST,
                amount,
                "USD",
                SourceType.API_VERIFIED,
                now,
                period_start=p_start,
                period_end=p_end,
                latency=DataLatency.DELAYED,
                is_final=is_closed_period(p_end, now, SETTLE),
            )
            for (p_start, p_end), amount in sorted(daily.items())
        )

        purchases = await self.api.get_json(
            f"{self.project_path}/purchases", params={"limit": PURCHASE_LIMIT}
        )
        payments: list[ProviderPayment] = []
        skipped_promotional = 0
        orders = purchases.get("orders") if isinstance(purchases, dict) else None
        for order in orders or []:
            order_type = str(order.get("order_type") or "")
            if order_type.lower() == "promotional":
                skipped_promotional += 1  # free credit, not money paid
                continue
            payments.append(
                ProviderPayment(
                    external_id=str(require(order, "order_id", "order_id")),
                    amount=to_decimal(require(order, "amount", "order amount"), "order amount"),
                    currency=str(require(order, "units", "order units")).upper(),
                    transaction_date=parse_iso_datetime(
                        require(order, "created", "created"), "created"
                    ),
                    # Deepgram does not document order_type values; keep it visible, don't guess.
                    kind=PaymentKind.OTHER,
                    description=f"Deepgram order (type: {order_type or 'unspecified'})",
                )
            )
        warnings = (
            (f"{skipped_promotional} promotional order(s) not recorded as payments",)
            if skipped_promotional
            else ()
        )
        return self.result(
            Operation.BILLING,
            observed_at=now,
            metrics=metrics,
            payments=tuple(payments),
            warnings=warnings,
        )

    async def _health(self) -> OperationResult:
        return self.result(Operation.HEALTH, health=await fetch_statuspage_health(self.status))
