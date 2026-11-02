from fastapi import APIRouter

from app.api.v1 import (
    admin,
    alerts,
    auth,
    aws,
    courses,
    dashboard,
    health,
    providers,
    settings,
    vouchers,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(providers.router)
api_router.include_router(settings.router)
api_router.include_router(aws.router)
api_router.include_router(alerts.alerts_router)
api_router.include_router(alerts.notifications_router)
api_router.include_router(dashboard.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(vouchers.router)
api_router.include_router(vouchers.integration_router)
api_router.include_router(courses.router)
api_router.include_router(courses.integration_router)
