"""OpenAI adapter (Admin API key).

Verified against the official OpenAPI spec (openai/openai-openapi, manual_spec):
  GET /v1/organization/usage/completions  start_time, end_time, bucket_width=1d, group_by,
                                          limit<=31, page
  GET /v1/organization/costs              start_time, end_time, bucket_width=1d, limit<=180, page
Both return {"object":"page","data":[bucket],"has_more":bool,"next_page":str|null}; each bucket
has start_time/end_time (unix s) and "results" (the schema names it "result"; both are read).
There is no supported endpoint for remaining prepaid credit.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.core.time import utcnow
from app.providers.base import CapabilityInfo, ProviderAdapter, require, require_list, to_decimal
from app.providers.common import from_unix, is_closed_period
from app.providers.http import ResilientHttpClient, Sleep
from app.providers.statuspage import fetch_statuspage_health
from app.providers.types import (
    DataLatency,
    MalformedResponseError,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    SourceType,
)

API_BASE = "https://api.openai.com"
STATUS_BASE = "https://status.openai.com"
MAX_PAGES = 20
# Daily buckets can still change shortly after a day ends.
SETTLE = timedelta(days=2)


def _bucket_results(bucket: Any) -> list[Any]:
    if isinstance(bucket, dict):
        for key in ("results", "result"):
            if isinstance(bucket.get(key), list):
                return bucket[key]
    raise MalformedResponseError("bucket results")


class OpenAIAdapter(ProviderAdapter):
    slug = "openai"
    capabilities = {
        Operation.BALANCE: CapabilityInfo(
            False,
            "OpenAI offers no supported API for remaining prepaid credit. Enter the balance "
            "manually; an estimate is then derived from the Costs API.",
        ),
        Operation.USAGE: CapabilityInfo(
            True, "Usage API (/v1/organization/usage/completions), daily buckets by model"
        ),
        Operation.BILLING: CapabilityInfo(
            True, "Costs API (/v1/organization/costs), daily buckets in USD"
        ),
        Operation.HEALTH: CapabilityInfo(True, "status.openai.com status JSON"),
    }

    def __init__(
        self,
        admin_key: str,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.api = ResilientHttpClient(
            self.slug,
            API_BASE,
            headers={"Authorization": f"Bearer {admin_key}"},
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

    async def _buckets(self, path: str, params: list[tuple[str, Any]]) -> list[Any]:
        buckets: list[Any] = []
        page: str | None = None
        for _ in range(MAX_PAGES):
            query = params + ([("page", page)] if page else [])
            payload = await self.api.get_json(path, params=query)
            buckets.extend(require_list(payload, "data", "data"))
            if not payload.get("has_more"):
                return buckets
            page = payload.get("next_page")
            if not isinstance(page, str) or not page:
                raise MalformedResponseError("has_more without next_page")
        raise MalformedResponseError("too many pages")

    async def _usage(self, start: datetime, end: datetime) -> OperationResult:
        now = utcnow()
        buckets = await self._buckets(
            "/v1/organization/usage/completions",
            [
                ("start_time", int(start.timestamp())),
                ("end_time", int(end.timestamp())),
                ("bucket_width", "1d"),
                ("group_by", "model"),
                ("limit", 31),
            ],
        )
        fields = (
            ("input_tokens", MetricType.INPUT_TOKENS, "tokens"),
            ("output_tokens", MetricType.OUTPUT_TOKENS, "tokens"),
            ("input_cached_tokens", MetricType.CACHED_INPUT_TOKENS, "tokens"),
            ("num_model_requests", MetricType.REQUESTS, "requests"),
        )
        metrics: list[Metric] = []
        for bucket in buckets:
            p_start = from_unix(require(bucket, "start_time", "start_time"), "start_time")
            p_end = from_unix(require(bucket, "end_time", "end_time"), "end_time")
            final = is_closed_period(p_end, now, SETTLE)
            totals: dict[MetricType, Decimal] = defaultdict(Decimal)
            for row in _bucket_results(bucket):
                model = row.get("model") or "unknown"
                for key, metric_type, unit in fields:
                    if key not in row:
                        if key == "input_cached_tokens":
                            continue  # optional in the spec
                        raise MalformedResponseError(f"usage result missing {key}")
                    value = to_decimal(row[key], key)
                    totals[metric_type] += value
                    metrics.append(
                        Metric(
                            self.slug,
                            metric_type,
                            value,
                            unit,
                            SourceType.API_VERIFIED,
                            now,
                            period_start=p_start,
                            period_end=p_end,
                            dimension=str(model)[:128],
                            is_final=final,
                        )
                    )
            for _, metric_type, unit in fields:
                # Explicit totals; an empty bucket means the API reported no usage that day.
                metrics.append(
                    Metric(
                        self.slug,
                        metric_type,
                        totals.get(metric_type, Decimal(0)),
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
        buckets = await self._buckets(
            "/v1/organization/costs",
            [
                ("start_time", int(start.timestamp())),
                ("end_time", int(end.timestamp())),
                ("bucket_width", "1d"),
                ("limit", 180),
            ],
        )
        metrics: list[Metric] = []
        for bucket in buckets:
            p_start = from_unix(require(bucket, "start_time", "start_time"), "start_time")
            p_end = from_unix(require(bucket, "end_time", "end_time"), "end_time")
            by_currency: dict[str, Decimal] = defaultdict(Decimal)
            for row in _bucket_results(bucket):
                amount = require(row, "amount", "amount")
                currency = str(require(amount, "currency", "amount.currency")).upper()
                by_currency[currency] += to_decimal(
                    require(amount, "value", "amount.value"), "amount.value"
                )
            if not by_currency:
                # Costs API amounts are documented in USD; an empty bucket is zero spend.
                by_currency["USD"] = Decimal(0)
            for currency, total in by_currency.items():
                metrics.append(
                    Metric(
                        self.slug,
                        MetricType.COST,
                        total,
                        currency,
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_end,
                        latency=DataLatency.DELAYED,
                        is_final=is_closed_period(p_end, now, SETTLE),
                    )
                )
        return self.result(Operation.BILLING, observed_at=now, metrics=tuple(metrics))

    async def _health(self) -> OperationResult:
        health = await fetch_statuspage_health(self.status)
        return self.result(Operation.HEALTH, health=health)
