from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_sync_service, require_read
from app.schemas.sync import ResourceList
from app.services.sync import SyncService

router = APIRouter(prefix="/aws", tags=["aws"], dependencies=[Depends(require_read)])


@router.get(
    "/resources",
    response_model=ResourceList,
    summary="Latest EC2 instance and EBS volume snapshot (read-only monitoring)",
)
def list_resources(service: SyncService = Depends(get_sync_service)) -> ResourceList:
    return service.latest_resources()
