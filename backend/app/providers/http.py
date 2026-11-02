"""HTTP client with timeouts, bounded retries, backoff and safe error classification."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.time import utcnow
from app.providers.types import (
    ErrorCode,
    MalformedResponseError,
    ProviderError,
    ProviderOperationError,
)

logger = logging.getLogger(__name__)
# httpx logs every request URL (including query strings) at INFO, and httpcore traces at
# DEBUG. Keep both quiet at the source; our own warnings log method + path only.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

Sleep = Callable[[float], Awaitable[None]]

_STATUS_ERRORS: dict[int, tuple[ErrorCode, str]] = {
    400: (ErrorCode.INVALID_REQUEST, "provider rejected the request (400)"),
    401: (ErrorCode.AUTH_FAILED, "authentication failed: check the API key (401)"),
    402: (ErrorCode.PERMISSION_DENIED, "payment required by provider (402)"),
    403: (ErrorCode.PERMISSION_DENIED, "API key lacks permission for this endpoint (403)"),
    404: (ErrorCode.NOT_FOUND, "endpoint or resource not found (404)"),
    422: (ErrorCode.INVALID_REQUEST, "provider rejected the request parameters (422)"),
    429: (ErrorCode.RATE_LIMITED, "rate limited by provider (429)"),
}


def classify_status(status: int) -> ProviderError:
    if status in _STATUS_ERRORS:
        code, message = _STATUS_ERRORS[status]
        return ProviderError(code, message, retryable=status == 429, http_status=status)
    if status >= 500:
        return ProviderError(
            ErrorCode.UPSTREAM_ERROR,
            f"provider error ({status})",
            retryable=True,
            http_status=status,
        )
    return ProviderError(
        ErrorCode.UNEXPECTED, f"unexpected HTTP status {status}", http_status=status
    )


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(raw) - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None


class ResilientHttpClient:
    """Thin wrapper over httpx.AsyncClient. Never logs headers, bodies or query strings."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        max_retry_after: float = 30.0,
    ) -> None:
        self.provider = provider
        self.max_retries = max_retries
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_retry_after = max_retry_after
        self._client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "headers": {"Accept": "application/json", **(headers or {})},
            "timeout": httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            "transport": transport,
            "follow_redirects": False,  # never forward credentials to another host
        }
        self._http: httpx.AsyncClient | None = None

    @property
    def _client(self) -> httpx.AsyncClient:
        # Created on first use: building the TLS context is slow and unused providers
        # (e.g. not polled yet) should not pay for it at startup.
        if self._http is None:
            self._http = httpx.AsyncClient(**self._client_kwargs)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * (2**attempt))
        return random.uniform(ceiling / 2, ceiling)  # noqa: S311 - jitter, not crypto

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
    ) -> Any:
        attempt = 0
        while True:
            error: ProviderError
            delay: float | None = None
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException:
                error = ProviderError(ErrorCode.TIMEOUT, "request timed out", retryable=True)
            except httpx.TransportError as exc:
                error = ProviderError(
                    ErrorCode.NETWORK_ERROR,
                    f"network error ({type(exc).__name__})",
                    retryable=True,
                )
            else:
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise MalformedResponseError("body is not valid JSON") from exc
                error = classify_status(response.status_code)
                if response.status_code == 429:
                    delay = retry_after_seconds(response)
                    if delay is not None and delay > self._max_retry_after:
                        # Provider wants us to back off longer than a poll should block.
                        error = ProviderError(
                            ErrorCode.RATE_LIMITED,
                            f"rate limited; provider asked to retry after {int(delay)}s",
                            retryable=False,
                            http_status=429,
                        )

            logger.warning(
                "provider request failed",
                extra={
                    "provider": self.provider,
                    "method": method,
                    "path": path,  # path only; query strings may contain sensitive values
                    "error_code": error.code.value,
                    "http_status": error.http_status,
                    "attempt": attempt + 1,
                },
            )
            if not error.retryable or attempt >= self.max_retries:
                raise ProviderOperationError(error)
            await self._sleep(delay if delay is not None else self._backoff(attempt))
            attempt += 1

    async def get_json(self, path: str, *, params: Any = None) -> Any:
        return await self.request_json("GET", path, params=params)

    async def post_json(self, path: str, *, json: Any = None, params: Any = None) -> Any:
        return await self.request_json("POST", path, json=json, params=params)
