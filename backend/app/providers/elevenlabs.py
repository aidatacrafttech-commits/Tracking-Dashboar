"""ElevenLabs adapter (``xi-api-key`` header).

Verified against elevenlabs.io/docs:
  GET /v1/user/subscription -> character_count, character_limit (credits; "characters" is the
      legacy name), next_character_count_reset_unix (s), current_overage{amount:str,currency},
      open_invoices[{amount_due_cents,...}], currency, tier, status
  GET /v1/usage/character-stats (DEPRECATED, no removal date) start_unix/end_unix in ms,
      metric, aggregation_interval -> {"time":[ms...], "usage":{"<key>":[values...]}}

The plan quota is credits, not money. The prepaid pay-as-you-go USD balance has no API.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.core.time import utcnow
from app.providers.base import CapabilityInfo, ProviderAdapter, require, require_list, to_decimal
from app.providers.common import day_start, from_unix, is_closed_period
from app.providers.http import ResilientHttpClient, Sleep
from app.providers.statuspage import fetch_statuspage_health
from app.providers.types import (
    MalformedResponseError,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    SourceType,
)

API_BASE = "https://api.elevenlabs.io"
STATUS_BASE = "https://status.elevenlabs.io"
CREDITS = "CREDITS"
SETTLE = timedelta(days=1)


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedResponseError(f"{what} is not an integer")
    return value


class ElevenLabsAdapter(ProviderAdapter):
    slug = "elevenlabs"
    capabilities = {
        Operation.BALANCE: CapabilityInfo(
            True,
            "Plan credit quota from /v1/user/subscription (credits, not money). Pay-as-you-go "
            "USD balance has no API.",
        ),
        Operation.USAGE: CapabilityInfo(
            True, "Daily credits from /v1/usage/character-stats (deprecated by ElevenLabs)"
        ),
        Operation.BILLING: CapabilityInfo(
            True, "Current overage and open invoices from /v1/user/subscription"
        ),
        Operation.HEALTH: CapabilityInfo(True, "status.elevenlabs.io Statuspage JSON"),
    }

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.api = ResilientHttpClient(
            self.slug,
            API_BASE,
            headers={"xi-api-key": api_key},
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

    async def _subscription(self) -> dict[str, Any]:
        payload = await self.api.get_json("/v1/user/subscription")
        if not isinstance(payload, dict):
            raise MalformedResponseError("subscription is not an object")
        return payload

    async def _balance(self) -> OperationResult:
        now = utcnow()
        sub = await self._subscription()
        used = _int(require(sub, "character_count", "character_count"), "character_count")
        limit = _int(require(sub, "character_limit", "character_limit"), "character_limit")
        reset_raw = sub.get("next_character_count_reset_unix")
        reset = from_unix(reset_raw, "next_character_count_reset_unix") if reset_raw else None
        metric = Metric(
            self.slug,
            MetricType.PLAN_QUOTA_REMAINING,
            Decimal(limit - used),  # may be negative when usage-based billing extends the plan
            CREDITS,
            SourceType.API_VERIFIED,
            now,
            details={
                "credit_limit": limit,
                "credits_used": used,
                "resets_at": reset.isoformat() if reset else None,
                "tier": sub.get("tier"),
                "status": sub.get("status"),
                "refresh_period": sub.get("character_refresh_period"),
                "usage_based_billing_limit": sub.get("max_credit_limit_extension"),
            },
        )
        return self.result(Operation.BALANCE, observed_at=now, metrics=(metric,))

    async def _usage(self, start: datetime, end: datetime) -> OperationResult:
        now = utcnow()
        payload = await self.api.get_json(
            "/v1/usage/character-stats",
            params={
                "start_unix": int(start.timestamp() * 1000),
                "end_unix": int(end.timestamp() * 1000),
                "metric": "credits",
                "aggregation_interval": "day",
                "breakdown_type": "none",
            },
        )
        times = require_list(payload, "time", "time")
        usage = require(payload, "usage", "usage")
        if not isinstance(usage, dict):
            raise MalformedResponseError("usage is not an object")
        totals = [Decimal(0)] * len(times)
        for series in usage.values():
            if not isinstance(series, list) or len(series) != len(times):
                raise MalformedResponseError("usage series length does not match time")
            for i, value in enumerate(series):
                totals[i] += to_decimal(value, "usage value")
        metrics = []
        for stamp, total in zip(times, totals, strict=True):
            p_start = day_start(from_unix(stamp, "time", millis=True))
            p_end = p_start + timedelta(days=1)
            metrics.append(
                Metric(
                    self.slug,
                    MetricType.CREDITS_USED,
                    total,
                    CREDITS,
                    SourceType.API_VERIFIED,
                    now,
                    period_start=p_start,
                    period_end=p_end,
                    is_final=is_closed_period(p_end, now, SETTLE),
                    details={"endpoint": "character-stats", "deprecated_endpoint": True},
                )
            )
        return self.result(
            Operation.USAGE,
            observed_at=now,
            metrics=tuple(metrics),
            warnings=("uses /v1/usage/character-stats, which ElevenLabs marks as deprecated",),
        )

    async def _billing(self, start: datetime, end: datetime) -> OperationResult:
        now = utcnow()
        sub = await self._subscription()
        # Point-in-time billing figures are stored as one observation per UTC day.
        p_start = day_start(now)
        p_end = p_start + timedelta(days=1)
        metrics: list[Metric] = []
        warnings: list[str] = []

        overage = sub.get("current_overage")
        if isinstance(overage, dict):
            metrics.append(
                Metric(
                    self.slug,
                    MetricType.OVERAGE,
                    to_decimal(require(overage, "amount", "current_overage.amount"), "overage"),
                    str(require(overage, "currency", "current_overage.currency")).upper(),
                    SourceType.API_VERIFIED,
                    now,
                    period_start=p_start,
                    period_end=p_end,
                    details={"kind": "daily_observation"},
                )
            )

        invoices = sub.get("open_invoices") or []
        if invoices:
            currency = sub.get("currency")
            if not isinstance(currency, str):
                warnings.append("open invoices present but subscription currency is unknown")
            else:
                cents = sum(
                    _int(require(inv, "amount_due_cents", "amount_due_cents"), "amount_due_cents")
                    for inv in invoices
                )
                metrics.append(
                    Metric(
                        self.slug,
                        MetricType.OPEN_INVOICE_AMOUNT,
                        Decimal(cents) / 100,
                        currency.upper(),
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_end,
                        details={"kind": "daily_observation", "open_invoice_count": len(invoices)},
                    )
                )
        return self.result(
            Operation.BILLING, observed_at=now, metrics=tuple(metrics), warnings=tuple(warnings)
        )

    async def _health(self) -> OperationResult:
        return self.result(Operation.HEALTH, health=await fetch_statuspage_health(self.status))
