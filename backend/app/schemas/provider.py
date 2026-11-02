from __future__ import annotations

from typing import Literal

from app.models.enums import BalanceCapability, UnitType
from app.providers.types import SourceType
from app.schemas.common import ApiModel, DecimalStr
from app.schemas.snapshot import BalanceSnapshotRead, Freshness
from app.schemas.sync import IntegrationRead
from app.schemas.threshold import ThresholdSummary


class ThresholdHeadroom(ApiModel):
    """Balance minus threshold, computed server-side with exact decimals.

    Only present when the latest balance of the threshold's kind is known and in the
    threshold's unit; never computed across currencies or from missing data.
    """

    amount: DecimalStr
    unit: str
    position: Literal["above", "at", "below"]
    balance_source_type: SourceType
    is_stale: bool


class ProviderRead(ApiModel):
    id: str  # provider slug, used in all /providers/{provider_id} paths
    display_name: str
    balance_capability: BalanceCapability
    balance_capability_note: str
    default_unit: str
    default_unit_type: UnitType
    stale_after_minutes: int
    status_page_url: str | None
    is_enabled: bool
    latest_balance: BalanceSnapshotRead | None
    latest_balance_freshness: Freshness | None
    threshold: ThresholdSummary | None
    integration: IntegrationRead | None = None
    threshold_headroom: ThresholdHeadroom | None = None
