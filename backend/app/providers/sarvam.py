"""Sarvam AI adapter.

Sarvam documents no public API for credits, usage/spend, workspace usage or payment history
(re-checked 2026-10-15 against docs.sarvam.ai/api/platform/billing, /api/platform/faq and the
full API reference index; GET https://api.sarvam.ai/usage returns 404). Their docs place all
of it in the dashboard only:
  remaining credits    Settings -> Billing ("Credits Left"; one balance per organisation)
  usage and spend      Settings -> Usage (organisation-wide, attributed per workspace)
  payment history      Settings -> Billing -> Payment History
Those operations are therefore unsupported here: the balance and payments are entered
manually, and nothing is scraped from the logged-in dashboard. Only the public status page
is polled, so no Sarvam API key is used or needed.
"""

from __future__ import annotations

import asyncio

import httpx

from app.providers.base import CapabilityInfo, ProviderAdapter
from app.providers.http import ResilientHttpClient, Sleep
from app.providers.statuspage import fetch_statuspage_health
from app.providers.types import Operation, OperationResult

STATUS_BASE = "https://status.sarvam.ai"
_NO_API = "Sarvam documents no public API for {what}; it is shown at dashboard.sarvam.ai {where}."


class SarvamAdapter(ProviderAdapter):
    slug = "sarvam"
    capabilities = {
        Operation.BALANCE: CapabilityInfo(
            False,
            _NO_API.format(what="remaining credits", where='Settings -> Billing ("Credits Left")')
            + " Record it here with Record current balance.",
        ),
        Operation.USAGE: CapabilityInfo(
            False,
            _NO_API.format(what="usage or spend", where="Settings -> Usage")
            + " It is not tracked here.",
        ),
        Operation.BILLING: CapabilityInfo(
            False,
            _NO_API.format(what="payment history", where="Settings -> Billing -> Payment History")
            + " Add payments here on the Payments page.",
        ),
        Operation.HEALTH: CapabilityInfo(True, "status.sarvam.ai status JSON"),
    }

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.status = ResilientHttpClient(
            self.slug, STATUS_BASE, timeout=timeout, max_retries=1, transport=transport, sleep=sleep
        )

    async def aclose(self) -> None:
        await self.status.aclose()

    async def _health(self) -> OperationResult:
        return self.result(Operation.HEALTH, health=await fetch_statuspage_health(self.status))
