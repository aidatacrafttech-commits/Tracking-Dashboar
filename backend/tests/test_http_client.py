from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app.providers.http import ResilientHttpClient, classify_status
from app.providers.types import ErrorCode, MalformedResponseError, ProviderOperationError
from tests.fakes import FakeHttp, SleepRecorder

URL = "https://api.example.test/v1/thing"
SECRET = "sk-test-SECRETVALUE-0123456789abcdef"


def client(fake: FakeHttp, sleep: SleepRecorder, retries: int = 2) -> ResilientHttpClient:
    return ResilientHttpClient(
        "example",
        "https://api.example.test",
        headers={"Authorization": f"Bearer {SECRET}"},
        max_retries=retries,
        transport=fake.transport,
        sleep=sleep,
    )


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _get(c: ResilientHttpClient, **kw):  # type: ignore[no-untyped-def]
    try:
        return await c.get_json("/v1/thing", **kw)
    finally:
        await c.aclose()


def test_success_returns_json_and_sends_auth() -> None:
    fake = FakeHttp().json(URL, {"ok": True})
    assert run(_get(client(fake, SleepRecorder()))) == {"ok": True}
    assert fake.requests[0].headers["Authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, ErrorCode.AUTH_FAILED),
        (403, ErrorCode.PERMISSION_DENIED),
        (404, ErrorCode.NOT_FOUND),
        (400, ErrorCode.INVALID_REQUEST),
    ],
)
def test_client_errors_are_not_retried(status: int, code: ErrorCode) -> None:
    fake = FakeHttp().json(URL, {"error": "nope"}, status=status)
    sleep = SleepRecorder()
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, sleep)))
    assert exc.value.error.code is code
    assert exc.value.error.http_status == status
    assert len(fake.requests) == 1 and sleep.delays == []


def test_rate_limit_honours_retry_after_then_succeeds() -> None:
    fake = FakeHttp().add(
        "GET",
        URL,
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json={"ok": 1}),
    )
    sleep = SleepRecorder()
    assert run(_get(client(fake, sleep))) == {"ok": 1}
    assert sleep.delays == [3.0]


def test_rate_limit_with_long_retry_after_fails_fast() -> None:
    fake = FakeHttp().add("GET", URL, httpx.Response(429, headers={"Retry-After": "600"}))
    sleep = SleepRecorder()
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, sleep)))
    assert exc.value.error.code is ErrorCode.RATE_LIMITED
    assert "600" in exc.value.error.message
    assert len(fake.requests) == 1 and sleep.delays == []


def test_server_errors_retry_with_bounded_exponential_backoff() -> None:
    fake = FakeHttp().add("GET", URL, httpx.Response(503))
    sleep = SleepRecorder()
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, sleep, retries=3)))
    assert exc.value.error.code is ErrorCode.UPSTREAM_ERROR
    assert len(fake.requests) == 4  # 1 + 3 retries, then stop
    # jittered within [ceiling/2, ceiling] for ceilings 0.5, 1, 2
    for delay, ceiling in zip(sleep.delays, (0.5, 1.0, 2.0), strict=True):
        assert ceiling / 2 <= delay <= ceiling


def test_timeout_and_network_errors_are_classified() -> None:
    fake = FakeHttp().add("GET", URL, httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, SleepRecorder(), retries=1)))
    assert exc.value.error.code is ErrorCode.TIMEOUT
    assert len(fake.requests) == 2

    fake = FakeHttp().add("GET", URL, httpx.ConnectError("refused"))
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, SleepRecorder(), retries=0)))
    assert exc.value.error.code is ErrorCode.NETWORK_ERROR


def test_transient_failure_then_success() -> None:
    fake = FakeHttp().add(
        "GET", URL, httpx.ConnectError("blip"), httpx.Response(200, json={"ok": 2})
    )
    assert run(_get(client(fake, SleepRecorder()))) == {"ok": 2}


def test_invalid_json_is_malformed() -> None:
    fake = FakeHttp().add("GET", URL, httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(MalformedResponseError):
        run(_get(client(fake, SleepRecorder())))


def test_redirects_are_not_followed() -> None:
    fake = FakeHttp().add(
        "GET", URL, httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    )
    with pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, SleepRecorder())))
    assert exc.value.error.code is ErrorCode.UNEXPECTED
    assert all(r.url.host == "api.example.test" for r in fake.requests)


def test_error_messages_and_logs_never_contain_secrets_or_bodies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"error": f"invalid key {SECRET}"}
    fake = FakeHttp().json(URL, body, status=401)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderOperationError) as exc:
        run(_get(client(fake, SleepRecorder()), params={"api_key": SECRET}))
    assert SECRET not in exc.value.error.message
    assert SECRET not in caplog.text
    assert "api_key" not in caplog.text  # query string never logged by our client


def test_classify_status_unknown() -> None:
    assert classify_status(418).code is ErrorCode.UNEXPECTED
    assert classify_status(500).retryable is True
