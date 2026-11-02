from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import ApplicationSetting
from app.models.enums import Actor


class SettingsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> ApplicationSetting | None:
        return self.session.get(ApplicationSetting, key)

    def upsert(self, key: str, value: Any, actor: Actor) -> ApplicationSetting:
        row = self.get(key)
        if row is None:
            row = ApplicationSetting(key=key, value=value, updated_by=actor)
            self.session.add(row)
        else:
            row.value = value
            row.updated_by = actor
        self.session.flush()
        return row
