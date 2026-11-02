"""Common adapter interface.

Every operation returns an ``OperationResult`` and never raises: provider failures are
classified into a ``ProviderError`` so one broken provider cannot affect the others.
Operations a provider cannot support return ``SyncStatus.UNSUPPORTED`` with the reason;
they never fabricate values.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from app.core.time import utcnow
from app.providers.types import (
    ErrorCode,
    MalformedResponseError,
    Operation,
    OperationResult,
    ProviderError,
    ProviderOperationError,
    SyncStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    supported: bool
    note: str


class ProviderAdapter(ABC):
    slug: ClassVar[str]
    # Static capability matrix. Operations missing here are unsupported.
    capabilities: ClassVar[dict[Operation, CapabilityInfo]]

    def supports(self, operation: Operation) -> bool:
        info = self.capabilities.get(operation)
        return bool(info and info.supported)

    # --- public operations ---------------------------------------------------------
    async def fetch_balance(self) -> OperationResult:
        return await self._dispatch(Operation.BALANCE, self._balance)

    async def fetch_usage(self, start: datetime, end: datetime) -> OperationResult:
        return await self._dispatch(Operation.USAGE, lambda: self._usage(start, end))

    async def fetch_billing_summary(self, start: datetime, end: datetime) -> OperationResult:
        return await self._dispatch(Operation.BILLING, lambda: self._billing(start, end))

    async def health_check(self) -> OperationResult:
        return await self._dispatch(Operation.HEALTH, self._health)

    async def fetch_resources(self) -> OperationResult:
        return await self._dispatch(Operation.RESOURCES, self._resources)

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network clients."""

    # --- overridable implementations --------------------------------------------------
    async def _balance(self) -> OperationResult:
        raise NotImplementedError

    async def _usage(self, start: datetime, end: datetime) -> OperationResult:
        raise NotImplementedError

    async def _billing(self, start: datetime, end: datetime) -> OperationResult:
        raise NotImplementedError

    async def _health(self) -> OperationResult:
        raise NotImplementedError

    async def _resources(self) -> OperationResult:
        raise NotImplementedError

    # --- helpers ---------------------------------------------------------------------------
    def result(self, operation: Operation, **kwargs: Any) -> OperationResult:
        kwargs.setdefault("status", SyncStatus.OK)
        kwargs.setdefault("observed_at", utcnow())
        return OperationResult(provider=self.slug, operation=operation, **kwargs)

    def unsupported(self, operation: Operation) -> OperationResult:
        info = self.capabilities.get(operation)
        note = info.note if info else "not supported by this provider"
        return self.result(
            operation,
            status=SyncStatus.UNSUPPORTED,
            error=ProviderError(ErrorCode.UNSUPPORTED, note),
        )

    def failure(self, operation: Operation, error: ProviderError) -> OperationResult:
        return self.result(operation, status=SyncStatus.ERROR, error=error)

    async def _dispatch(
        self, operation: Operation, call: Callable[[], Awaitable[OperationResult]]
    ) -> OperationResult:
        if not self.supports(operation):
            return self.unsupported(operation)
        try:
            return await call()
        except ProviderOperationError as exc:
            return self.failure(operation, exc.error)
        except (KeyError, TypeError, ValueError, InvalidOperation, IndexError) as exc:
            # Parsing failed on a shape we did not expect; never echo the payload.
            logger.warning(
                "malformed provider response",
                extra={
                    "provider": self.slug,
                    "operation": operation.value,
                    "error_type": type(exc).__name__,
                },
            )
            return self.failure(
                operation,
                ProviderError(
                    ErrorCode.MALFORMED_RESPONSE,
                    f"unexpected response shape ({type(exc).__name__})",
                ),
            )
        except Exception as exc:  # isolation: one adapter bug must not stop polling
            logger.exception(
                "adapter error", extra={"provider": self.slug, "operation": operation.value}
            )
            return self.failure(
                operation,
                ProviderError(
                    ErrorCode.UNEXPECTED, f"internal adapter error ({type(exc).__name__})"
                ),
            )


# --- parsing helpers shared by adapters ----------------------------------------------------


def to_decimal(value: Any, what: str) -> Decimal:
    """Exact Decimal from a JSON number or string. bool/None/NaN are rejected."""
    if isinstance(value, bool) or value is None:
        raise MalformedResponseError(f"{what} is not a number")
    try:
        # str() of a JSON float gives the shortest round-trip repr, e.g. 0.1 -> "0.1".
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MalformedResponseError(f"{what} is not a number") from exc
    if not result.is_finite():
        raise MalformedResponseError(f"{what} is not finite")
    return result


def require(mapping: Any, key: str, what: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise MalformedResponseError(f"missing {what}")
    return mapping[key]


def require_list(mapping: Any, key: str, what: str) -> list[Any]:
    value = require(mapping, key, what)
    if not isinstance(value, list):
        raise MalformedResponseError(f"{what} is not a list")
    return value
