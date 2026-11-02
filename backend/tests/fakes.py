"""Test doubles: a routing httpx MockTransport and official-doc example payloads.

No test in this suite performs real network I/O or consumes provider credits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

Responder = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class FakeHttp:
    """Routes (method, host, path) to queued responses and records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str, str], list[Responder]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, *responses: Responder) -> FakeHttp:
        parsed = httpx.URL(url)
        self.routes[(method, parsed.host, parsed.path)] = list(responses)
        return self

    def json(self, url: str, payload: Any, status: int = 200, method: str = "GET") -> FakeHttp:
        return self.add(method, url, httpx.Response(status, json=payload))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.host, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"error": "no fake route"})
        queue = self.routes[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


STATUS_OK = {
    "page": {"id": "x", "name": "Status"},
    "status": {"indicator": "none", "description": "All Systems Operational"},
}

# --- verbatim shapes from official documentation ------------------------------------------

OPENAI_COSTS = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": 1730419200,
            "end_time": 1730505600,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.06, "currency": "usd"},
                    "line_item": None,
                    "project_id": None,
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}

OPENAI_USAGE_RESULT = {
    "object": "organization.usage.completions.result",
    "input_tokens": 1000,
    "output_tokens": 500,
    "input_cached_tokens": 800,
    "input_audio_tokens": 0,
    "output_audio_tokens": 0,
    "num_model_requests": 5,
    "project_id": None,
    "user_id": None,
    "api_key_id": None,
    "model": None,
    "batch": None,
}

DEEPGRAM_BALANCES = {
    "balances": [
        {
            "balance_id": "b7f3c9d2-4a1e-4f8b-9c3d-2e5a7f8b9c1d",
            "amount": 1250.75,
            "units": "USD",
            "purchase_order_id": "PO-2024-04567",
        }
    ]
}

DEEPGRAM_USAGE = {
    "start": "2025-01-16",
    "end": "2025-01-23",
    "resolution": {"units": "day", "amount": 1},
    "results": [
        {
            "hours": 1619.7242069444444,
            "total_hours": 1621.7395791666668,
            "agent_hours": 41.33564388888889,
            "tokens_in": 0,
            "tokens_out": 0,
            "tts_characters": 9158866,
            "requests": 373381,
            "grouping": {
                "start": "2025-01-16",
                "end": "2025-01-16",
                "accessor": "123456789012345678901234",
                "endpoint": "listen",
                "feature_set": "punctuate",
                "models": ["Nova-2"],
                "method": "async",
                "tags": ["tag1", "tag2"],
                "deployment": "self-hosted",
            },
        }
    ],
}

DEEPGRAM_BILLING = {
    "start": "2025-01-16",
    "end": "2025-01-23",
    "resolution": {"units": "day", "amount": 1},
    "results": [
        {
            "dollars": 0.25,
            "grouping": {
                "start": "2025-01-16",
                "end": "2025-01-16",
                "accessor": "123456789012345678901234",
                "deployment": "hosted",
                "line_item": "streaming::nova-3",
                "tags": ["tag1", "tag2"],
            },
        }
    ],
}

DEEPGRAM_PURCHASES = {
    "orders": [
        {
            "order_id": "025e19ba-b6d9-4a04-9f99-4fe715aca5f1",
            "expiration": "2026-03-04T00:00:00Z",
            "created": "2023-02-21T21:13:40.014373Z",
            "amount": 150,
            "units": "usd",
            "order_type": "promotional",
        },
        {
            "order_id": "9a1c-paid-order",
            "expiration": "2027-03-04T00:00:00Z",
            "created": "2026-09-01T10:00:00Z",
            "amount": 200.5,
            "units": "usd",
            "order_type": "purchase",
        },
    ]
}

ELEVENLABS_SUBSCRIPTION = {
    "tier": "starter",
    "character_count": 1000,
    "character_limit": 10000,
    "max_character_limit_extension": 10000,
    "max_credit_limit_extension": 10000,
    "can_extend_character_limit": True,
    "allowed_to_extend_character_limit": True,
    "voice_slots_used": 1,
    "professional_voice_slots_used": 0,
    "professional_voice_slots_used_in_workspace": 0,
    "voice_limit": 10,
    "voice_add_edit_counter": 0,
    "professional_voice_limit": 1,
    "can_extend_voice_limit": True,
    "can_use_instant_voice_cloning": True,
    "can_use_professional_voice_cloning": True,
    "current_overage": {"amount": "0", "currency": "usd"},
    "status": "active",
    "open_invoices": [
        {
            "amount_due_cents": 1000,
            "discounts": [{"discount_percent_off": 20}],
            "next_payment_attempt_unix": 1738356858,
            "payment_intent_status": "processing",
            "payment_intent_statusses": ["processing", "succeeded"],
            "subtotal_cents": 900,
            "tax_cents": 100,
        }
    ],
    "has_open_invoices": True,
    "next_character_count_reset_unix": 1738356858,
    "currency": "usd",
    "billing_period": "monthly_period",
    "character_refresh_period": "monthly_period",
    "next_invoice": None,
    "has_used_starter_coupon_on_account": False,
    "has_used_creator_coupon_on_account": False,
    "is_eligible_for_starter_promo": False,
}

ELEVENLABS_CHARACTER_STATS = {"time": [1738252091000, 1739404800000], "usage": {"All": [49, 1053]}}
