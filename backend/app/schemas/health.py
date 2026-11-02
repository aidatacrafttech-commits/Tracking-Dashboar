from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.common import ApiModel


class HealthRead(ApiModel):
    status: Literal["ok"]
    version: str
    time: datetime


class ReadinessRead(ApiModel):
    status: Literal["ready", "not_ready"]
    # ok | warning (degraded but serving) | failed | pending_migrations | disabled
    checks: dict[str, Literal["ok", "warning", "failed", "pending_migrations", "disabled"]]
