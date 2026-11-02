from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_settings_service, owner_context, require_read
from app.schemas.settings import SettingsRead, SettingsUpdate
from app.services.context import RequestContext
from app.services.settings import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "",
    response_model=SettingsRead,
    dependencies=[Depends(require_read)],
    summary="Display preferences (no secrets)",
)
def get_app_settings(service: SettingsService = Depends(get_settings_service)) -> SettingsRead:
    return service.get()


@router.put("", response_model=SettingsRead, summary="Update display preferences (owner only)")
def put_app_settings(
    body: SettingsUpdate,
    ctx: RequestContext = Depends(owner_context),
    service: SettingsService = Depends(get_settings_service),
) -> SettingsRead:
    return service.update(body, ctx)
