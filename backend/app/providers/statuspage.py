"""Public status-page health via the Atlassian Statuspage-compatible JSON API.

GET {status_host}/api/v2/status.json -> {"status": {"indicator": ..., "description": ...}}
This client is separate from the authenticated API client so provider credentials are
never sent to the status host.
"""

from __future__ import annotations

import time

from app.models.enums import HealthSource, HealthStatus
from app.providers.base import require
from app.providers.http import ResilientHttpClient
from app.providers.types import HealthResult, MalformedResponseError

INDICATOR_MAP = {
    "none": HealthStatus.OPERATIONAL,
    "minor": HealthStatus.DEGRADED,
    "major": HealthStatus.PARTIAL_OUTAGE,
    "critical": HealthStatus.MAJOR_OUTAGE,
    "maintenance": HealthStatus.MAINTENANCE,
}


async def fetch_statuspage_health(client: ResilientHttpClient) -> HealthResult:
    started = time.perf_counter()
    payload = await client.get_json("/api/v2/status.json")
    status = require(payload, "status", "status object")
    indicator = require(status, "indicator", "status.indicator")
    if not isinstance(indicator, str):
        raise MalformedResponseError("status.indicator is not a string")
    description = status.get("description")
    return HealthResult(
        status=INDICATOR_MAP.get(indicator, HealthStatus.UNKNOWN),
        source=HealthSource.STATUS_PAGE,
        indicator=indicator[:32],
        description=str(description)[:500] if description is not None else None,
        http_status=200,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
