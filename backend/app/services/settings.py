from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import SENSITIVE_KEY_RE
from app.core.time import validate_timezone_name
from app.db.session import transaction
from app.repositories.audit import AuditLogRepository
from app.repositories.settings import SettingsRepository
from app.schemas.settings import SettingsRead, SettingsUpdate
from app.services.context import RequestContext

DISPLAY_TIMEZONE = "display_timezone"
# Only these keys may ever be written to application_settings.
VOUCHER_USER_LIMIT = "voucher_user_limit"  # written by VoucherService
ALLOWED_KEYS = frozenset({DISPLAY_TIMEZONE, VOUCHER_USER_LIMIT})


def _assert_storable(key: str) -> None:
    if key not in ALLOWED_KEYS or SENSITIVE_KEY_RE.search(key):
        raise ValueError(f"setting key {key!r} may not be stored in the database")


class SettingsService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = SettingsRepository(session)
        self.audit = AuditLogRepository(session)

    def display_timezone(self) -> str:
        row = self.repo.get(DISPLAY_TIMEZONE)
        if row is not None and isinstance(row.value, str):
            try:
                return validate_timezone_name(row.value)
            except ValueError:
                pass  # tzdata changed underneath us; fall back to the configured default
        return self.settings.default_display_timezone

    def get(self) -> SettingsRead:
        return SettingsRead(
            display_timezone=self.display_timezone(),
            supported_currencies=self.settings.supported_currencies,
        )

    def update(self, body: SettingsUpdate, ctx: RequestContext) -> SettingsRead:
        _assert_storable(DISPLAY_TIMEZONE)
        with transaction(self.session):
            before = self.display_timezone()
            self.repo.upsert(DISPLAY_TIMEZONE, body.display_timezone, ctx.actor)
            self.audit.record(
                actor=ctx.actor,
                action="settings.update",
                entity_type="application_setting",
                entity_id=DISPLAY_TIMEZONE,
                request_id=ctx.request_id,
                client_ip=ctx.client_ip,
                changes={"before": before, "after": body.display_timezone},
            )
        return self.get()
