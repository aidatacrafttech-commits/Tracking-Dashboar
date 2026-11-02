from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.models.enums import HealthStatus, PaymentKind
from app.providers.deepgram import DeepgramAdapter
from app.providers.elevenlabs import ElevenLabsAdapter
from app.providers.openai import OpenAIAdapter
from app.providers.sarvam import SarvamAdapter
from app.providers.types import (
    DataLatency,
    ErrorCode,
    Metric,
    MetricType,
    OperationResult,
    SourceType,
    SyncStatus,
)
from tests.fakes import (
    DEEPGRAM_BALANCES,
    DEEPGRAM_BILLING,
    DEEPGRAM_PURCHASES,
    DEEPGRAM_USAGE,
    ELEVENLABS_CHARACTER_STATS,
    ELEVENLABS_SUBSCRIPTION,
    OPENAI_COSTS,
    OPENAI_USAGE_RESULT,
    STATUS_OK,
    FakeHttp,
    SleepRecorder,
)

OPENAI_KEY = "sk-admin-test-0123456789abcdefghij"
DG_KEY = "dg-test-key-0123456789abcdef0123456789"
EL_KEY = "el-test-key-0123456789abcdef0123456789"
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def call(adapter, method: str, *args: Any) -> OperationResult:  # type: ignore[no-untyped-def]
    try:
        return await getattr(adapter, method)(*args)
    finally:
        await adapter.aclose()


def metrics_of(
    result: OperationResult, metric_type: MetricType, dimension: str = ""
) -> list[Metric]:
    return [m for m in result.metrics if m.metric_type is metric_type and m.dimension == dimension]


# --- OpenAI ------------------------------------------------------------------------------------


def openai(fake: FakeHttp) -> OpenAIAdapter:
    return OpenAIAdapter(OPENAI_KEY, transport=fake.transport, sleep=SleepRecorder())


def test_openai_balance_is_unsupported_without_any_request() -> None:
    fake = FakeHttp()
    result = run(call(openai(fake), "fetch_balance"))
    assert result.status is SyncStatus.UNSUPPORTED
    assert result.error is not None and result.error.code is ErrorCode.UNSUPPORTED
    assert "manually" in result.error.message
    assert result.metrics == () and fake.requests == []


def test_openai_costs_exact_decimal_and_delayed() -> None:
    fake = FakeHttp().json("https://api.openai.com/v1/organization/costs", OPENAI_COSTS)
    result = run(call(openai(fake), "fetch_billing_summary", START, END))
    assert result.status is SyncStatus.OK
    [cost] = result.metrics
    assert cost.metric_type is MetricType.COST
    assert cost.value == Decimal("0.06") and isinstance(cost.value, Decimal)
    assert cost.unit == "USD"
    assert cost.source_type is SourceType.API_VERIFIED
    assert cost.latency is DataLatency.DELAYED
    assert cost.period_start == datetime(2024, 11, 1, tzinfo=UTC)
    assert cost.is_final is True

    req = fake.requests[0]
    assert req.headers["Authorization"] == f"Bearer {OPENAI_KEY}"
    assert req.url.params["bucket_width"] == "1d"
    assert req.url.params["start_time"] == str(int(START.timestamp()))
    assert req.url.params["end_time"] == str(int(END.timestamp()))


def test_openai_empty_cost_bucket_is_zero_spend() -> None:
    payload = copy.deepcopy(OPENAI_COSTS)
    payload["data"][0]["results"] = []
    fake = FakeHttp().json("https://api.openai.com/v1/organization/costs", payload)
    [cost] = run(call(openai(fake), "fetch_billing_summary", START, END)).metrics
    assert cost.value == Decimal(0) and cost.unit == "USD"


def test_openai_usage_by_model_with_totals_and_pagination() -> None:
    page1 = {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1730419200,
                "end_time": 1730505600,
                "results": [
                    {**OPENAI_USAGE_RESULT, "model": "gpt-4o-mini"},
                    {**OPENAI_USAGE_RESULT, "model": "gpt-5.6-luna", "input_tokens": 50},
                ],
            }
        ],
        "has_more": True,
        "next_page": "page_AAAAAGdGxdEiJdKOAAAAAGcqsYA=",
    }
    # second page uses the schema's singular "result" key; both must be accepted
    page2 = {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 1730505600,
                "end_time": 1730592000,
                "result": [OPENAI_USAGE_RESULT],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    fake = FakeHttp().add(
        "GET",
        "https://api.openai.com/v1/organization/usage/completions",
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    )
    result = run(call(openai(fake), "fetch_usage", START, END))
    assert result.status is SyncStatus.OK
    first_day = [
        m
        for m in metrics_of(result, MetricType.INPUT_TOKENS)
        if m.period_start == datetime(2024, 11, 1, tzinfo=UTC)
    ]
    assert [m.value for m in first_day] == [Decimal(1050)]  # total across models
    assert metrics_of(result, MetricType.INPUT_TOKENS, "gpt-5.6-luna")[0].value == Decimal(50)
    assert metrics_of(result, MetricType.REQUESTS, "unknown")[0].value == Decimal(5)  # null model

    assert fake.requests[0].url.params.get_list("group_by") == ["model"]
    assert "page" not in fake.requests[0].url.params
    assert fake.requests[1].url.params["page"] == "page_AAAAAGdGxdEiJdKOAAAAAGcqsYA="


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"object": "page"}, "missing data"),
        ({"data": [{"start_time": 1, "end_time": 2}], "has_more": False}, "no results key"),
        ({"data": [], "has_more": True, "next_page": None}, "has_more without cursor"),
        (
            {
                "data": [
                    {
                        "start_time": 1730419200,
                        "end_time": 1730505600,
                        "results": [{"amount": {"value": "abc", "currency": "usd"}}],
                    }
                ],
                "has_more": False,
            },
            "non-numeric amount",
        ),
        ([1, 2, 3], "not an object"),
    ],
)
def test_openai_malformed_responses(payload: Any, why: str) -> None:
    fake = FakeHttp().json("https://api.openai.com/v1/organization/costs", payload)
    result = run(call(openai(fake), "fetch_billing_summary", START, END))
    assert result.status is SyncStatus.ERROR, why
    assert result.error is not None and result.error.code is ErrorCode.MALFORMED_RESPONSE
    assert result.metrics == ()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.AUTH_FAILED),
        (403, ErrorCode.PERMISSION_DENIED),
        (429, ErrorCode.RATE_LIMITED),
    ],
)
def test_openai_http_errors(status: int, code: ErrorCode) -> None:
    fake = FakeHttp().json(
        "https://api.openai.com/v1/organization/usage/completions",
        {"error": {"message": f"bad key {OPENAI_KEY}"}},
        status=status,
    )
    result = run(call(openai(fake), "fetch_usage", START, END))
    assert result.status is SyncStatus.ERROR
    assert result.error is not None and result.error.code is code
    assert OPENAI_KEY not in result.error.message


def test_openai_health_uses_status_page_without_credentials() -> None:
    fake = FakeHttp().json("https://status.openai.com/api/v2/status.json", STATUS_OK)
    result = run(call(openai(fake), "health_check"))
    assert result.health is not None and result.health.status is HealthStatus.OPERATIONAL
    assert "authorization" not in {k.lower() for k in fake.requests[0].headers}


@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        ("none", HealthStatus.OPERATIONAL),
        ("minor", HealthStatus.DEGRADED),
        ("major", HealthStatus.PARTIAL_OUTAGE),
        ("critical", HealthStatus.MAJOR_OUTAGE),
        ("something-new", HealthStatus.UNKNOWN),
    ],
)
def test_statuspage_indicator_mapping(indicator: str, expected: HealthStatus) -> None:
    fake = FakeHttp().json(
        "https://status.openai.com/api/v2/status.json",
        {"status": {"indicator": indicator, "description": "x"}},
    )
    assert run(call(openai(fake), "health_check")).health.status is expected  # type: ignore[union-attr]


def test_statuspage_timeout_is_an_error_not_a_crash() -> None:
    fake = FakeHttp().add(
        "GET", "https://status.openai.com/api/v2/status.json", httpx.ConnectTimeout("down")
    )
    result = run(call(openai(fake), "health_check"))
    assert result.status is SyncStatus.ERROR
    assert result.error is not None and result.error.code is ErrorCode.TIMEOUT


# --- Deepgram ----------------------------------------------------------------------------------

DG = "https://api.deepgram.com/v1/projects/proj-123"


def deepgram(fake: FakeHttp) -> DeepgramAdapter:
    return DeepgramAdapter(DG_KEY, "proj-123", transport=fake.transport, sleep=SleepRecorder())


def test_deepgram_balance_uses_token_auth() -> None:
    fake = FakeHttp().json(f"{DG}/balances", DEEPGRAM_BALANCES)
    result = run(call(deepgram(fake), "fetch_balance"))
    [balance] = result.metrics
    assert balance.metric_type is MetricType.PREPAID_BALANCE
    assert balance.value == Decimal("1250.75") and balance.unit == "USD"
    assert balance.source_type is SourceType.API_VERIFIED
    assert fake.requests[0].headers["Authorization"] == f"Token {DG_KEY}"


def test_deepgram_empty_balances_is_zero() -> None:
    fake = FakeHttp().json(f"{DG}/balances", {"balances": []})
    [balance] = run(call(deepgram(fake), "fetch_balance")).metrics
    assert balance.value == Decimal(0)


@pytest.mark.parametrize(
    "payload",
    [{}, {"balances": None}, {"data": {"balance": 5}}, [], {"balances": "1.00"}],
    ids=["field-missing", "null", "unexpected-shape", "json-list", "not-a-list"],
)
def test_deepgram_malformed_balance_is_unknown_not_zero(payload: object) -> None:
    """Only an explicit empty list means $0. Anything else is a malformed response: no
    balance is recorded, so no false low-balance alert can follow (production audit)."""
    fake = FakeHttp().json(f"{DG}/balances", payload)
    result = run(call(deepgram(fake), "fetch_balance"))
    assert result.metrics == ()
    assert result.error is not None and result.error.code is ErrorCode.MALFORMED_RESPONSE


def test_deepgram_balance_permission_denied() -> None:
    # Balances need billing:read (owner/admin key); a member key gets 403.
    fake = FakeHttp().json(f"{DG}/balances", {"err_code": "FORBIDDEN"}, status=403)
    result = run(call(deepgram(fake), "fetch_balance"))
    assert result.error is not None and result.error.code is ErrorCode.PERMISSION_DENIED


def test_deepgram_usage_daily_periods() -> None:
    fake = FakeHttp().json(f"{DG}/usage/breakdown", DEEPGRAM_USAGE)
    result = run(call(deepgram(fake), "fetch_usage", START, END))
    hours = metrics_of(result, MetricType.AUDIO_HOURS)[0]
    assert hours.value == Decimal("1619.7242069444444")
    assert hours.period_start == datetime(2025, 1, 16, tzinfo=UTC)
    assert hours.period_end == datetime(2025, 1, 17, tzinfo=UTC)  # inclusive end + 1 day
    assert metrics_of(result, MetricType.REQUESTS)[0].value == Decimal(373381)
    assert fake.requests[0].url.params["start"] == "2026-09-01"
    assert fake.requests[0].url.params["end"] == "2026-10-01"


def test_deepgram_billing_costs_and_verified_purchases() -> None:
    fake = (
        FakeHttp()
        .json(f"{DG}/billing/breakdown", DEEPGRAM_BILLING)
        .json(f"{DG}/purchases", DEEPGRAM_PURCHASES)
    )
    result = run(call(deepgram(fake), "fetch_billing_summary", START, END))
    [cost] = result.metrics
    assert cost.value == Decimal("0.25") and cost.unit == "USD"
    [payment] = result.payments  # promotional order is free credit, not a payment
    assert payment.external_id == "9a1c-paid-order"
    assert payment.amount == Decimal("200.5") and payment.currency == "USD"
    assert payment.kind is PaymentKind.OTHER
    assert payment.transaction_date == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert any("promotional" in w for w in result.warnings)


def test_deepgram_usage_malformed_grouping() -> None:
    bad = copy.deepcopy(DEEPGRAM_USAGE)
    bad["results"][0]["grouping"]["start"] = "not-a-date"
    fake = FakeHttp().json(f"{DG}/usage/breakdown", bad)
    result = run(call(deepgram(fake), "fetch_usage", START, END))
    assert result.error is not None and result.error.code is ErrorCode.MALFORMED_RESPONSE


# --- ElevenLabs --------------------------------------------------------------------------------

EL = "https://api.elevenlabs.io"


def elevenlabs(fake: FakeHttp) -> ElevenLabsAdapter:
    return ElevenLabsAdapter(EL_KEY, transport=fake.transport, sleep=SleepRecorder())


def test_elevenlabs_quota_is_credits_not_money() -> None:
    fake = FakeHttp().json(f"{EL}/v1/user/subscription", ELEVENLABS_SUBSCRIPTION)
    [quota] = run(call(elevenlabs(fake), "fetch_balance")).metrics
    assert quota.metric_type is MetricType.PLAN_QUOTA_REMAINING
    assert quota.value == Decimal(9000)
    assert quota.unit == "CREDITS"
    assert quota.details is not None
    assert quota.details["resets_at"] == "2025-01-31T20:54:18+00:00"
    assert fake.requests[0].headers["xi-api-key"] == EL_KEY


def test_elevenlabs_usage_sums_series_and_flags_deprecation() -> None:
    stats = {
        "time": ELEVENLABS_CHARACTER_STATS["time"],
        "usage": {"All": [49, 1053], "Other": [1, 7]},
    }
    fake = FakeHttp().json(f"{EL}/v1/usage/character-stats", stats)
    result = run(call(elevenlabs(fake), "fetch_usage", START, END))
    values = [m.value for m in result.metrics]
    assert values == [Decimal(50), Decimal(1060)]
    assert result.metrics[0].period_start == datetime(2025, 1, 30, tzinfo=UTC)
    assert any("deprecated" in w for w in result.warnings)
    params = fake.requests[0].url.params
    assert params["start_unix"] == str(int(START.timestamp() * 1000))  # milliseconds
    assert params["metric"] == "credits"


def test_elevenlabs_usage_mismatched_series_is_malformed() -> None:
    fake = FakeHttp().json(
        f"{EL}/v1/usage/character-stats", {"time": [1, 2], "usage": {"All": [1]}}
    )
    result = run(call(elevenlabs(fake), "fetch_usage", START, END))
    assert result.error is not None and result.error.code is ErrorCode.MALFORMED_RESPONSE


def test_elevenlabs_billing_overage_and_invoices() -> None:
    fake = FakeHttp().json(f"{EL}/v1/user/subscription", ELEVENLABS_SUBSCRIPTION)
    result = run(call(elevenlabs(fake), "fetch_billing_summary", START, END))
    overage = metrics_of(result, MetricType.OVERAGE)[0]
    assert overage.value == Decimal(0) and overage.unit == "USD"
    invoice = metrics_of(result, MetricType.OPEN_INVOICE_AMOUNT)[0]
    assert invoice.value == Decimal("10") and invoice.unit == "USD"


def test_elevenlabs_invoices_without_currency_are_not_guessed() -> None:
    sub = {**ELEVENLABS_SUBSCRIPTION, "currency": None}
    fake = FakeHttp().json(f"{EL}/v1/user/subscription", sub)
    result = run(call(elevenlabs(fake), "fetch_billing_summary", START, END))
    assert metrics_of(result, MetricType.OPEN_INVOICE_AMOUNT) == []
    assert any("currency is unknown" in w for w in result.warnings)


def test_elevenlabs_missing_quota_field_is_malformed() -> None:
    sub = {k: v for k, v in ELEVENLABS_SUBSCRIPTION.items() if k != "character_limit"}
    fake = FakeHttp().json(f"{EL}/v1/user/subscription", sub)
    result = run(call(elevenlabs(fake), "fetch_balance"))
    assert result.error is not None and result.error.code is ErrorCode.MALFORMED_RESPONSE


# --- Sarvam --------------------------------------------------------------------------------------


def test_sarvam_reports_unsupported_and_only_polls_status_page() -> None:
    fake = FakeHttp().json("https://status.sarvam.ai/api/v2/status.json", STATUS_OK)
    adapter = SarvamAdapter(transport=fake.transport, sleep=SleepRecorder())

    async def go() -> list[OperationResult]:
        try:
            return [
                await adapter.fetch_balance(),
                await adapter.fetch_usage(START, END),
                await adapter.fetch_billing_summary(START, END),
                await adapter.health_check(),
            ]
        finally:
            await adapter.aclose()

    balance, usage, billing, health = run(go())
    for result in (balance, usage, billing):
        assert result.status is SyncStatus.UNSUPPORTED and result.metrics == ()
    assert health.status is SyncStatus.OK
    assert [r.url.host for r in fake.requests] == ["status.sarvam.ai"]


# --- Metric invariants ---------------------------------------------------------------------------


def test_metric_rejects_float_and_silent_zero() -> None:
    now = datetime.now(UTC)
    with pytest.raises(TypeError):
        Metric("x", MetricType.COST, 0.1, "USD", SourceType.API_VERIFIED, now)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        Metric("x", MetricType.PREPAID_BALANCE, None, "USD", SourceType.API_VERIFIED, now)
    ok = Metric(
        "x",
        MetricType.PREPAID_BALANCE,
        None,
        "USD",
        SourceType.UNAVAILABLE,
        now,
        unavailable_reason="no API",
    )
    assert ok.value is None
