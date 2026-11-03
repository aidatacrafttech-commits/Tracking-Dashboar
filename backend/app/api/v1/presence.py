"""App presence: the AdhyaapakAI mobile app reports while it is open (see services/presence.py).

POST /api/v1/integrations/presence/heartbeat   Authorization: Bearer <the app's login token>
    {"state": "active"}       every 30 s while the app is in the foreground
    {"state": "background"}   when the app goes to the background or closes
    -> 204. 401 for a missing, forged or expired token, and while ADHYAAPAK_JWT_SECRET is unset.

Dashboard sessions and the owner/viewer tokens are not accepted here, and the response
carries nothing. The token is never logged.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import get_settings
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.time import utcnow
from app.schemas.common import InputModel
from app.services.presence import verify_app_token

router = APIRouter(prefix="/integrations/presence", tags=["app presence"])
_bearer = HTTPBearer(auto_error=False)


class Heartbeat(InputModel):
    state: Literal["active", "background"] = "active"


@router.post("/heartbeat", status_code=204, response_class=Response)
def heartbeat(
    body: Heartbeat,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Response:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthenticationError("The app's login token is required", code="app_token_required")
    if settings.adhyaapak_jwt_secret is None:
        # 401 like the other integration APIs when switched off (never a hint of a credential).
        raise AuthenticationError(
            "Live presence is not enabled (set ADHYAAPAK_JWT_SECRET on the dashboard server)",
            code="presence_not_configured",
        )
    user_id = verify_app_token(
        credentials.credentials, settings.adhyaapak_jwt_secret.get_secret_value(), utcnow()
    )
    tracker = request.app.state.presence
    if body.state == "active":
        tracker.beat(user_id)
    else:
        tracker.leave(user_id)
    return Response(status_code=204)
