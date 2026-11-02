from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ThresholdConfig


class ThresholdRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_for_provider(self, provider_id: int) -> ThresholdConfig | None:
        return self.session.scalar(
            select(ThresholdConfig).where(ThresholdConfig.provider_id == provider_id)
        )

    def for_providers(self, provider_ids: list[int]) -> dict[int, ThresholdConfig]:
        if not provider_ids:
            return {}
        rows = self.session.scalars(
            select(ThresholdConfig).where(ThresholdConfig.provider_id.in_(provider_ids))
        )
        return {row.provider_id: row for row in rows}

    def add(self, threshold: ThresholdConfig) -> ThresholdConfig:
        self.session.add(threshold)
        self.session.flush()
        return threshold
