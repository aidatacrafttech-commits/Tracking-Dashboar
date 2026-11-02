from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import Actor


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who is acting, carried into services for audit logging."""

    actor: Actor
    request_id: str | None = None
    client_ip: str | None = None


SYSTEM_CONTEXT = RequestContext(actor=Actor.SYSTEM)
