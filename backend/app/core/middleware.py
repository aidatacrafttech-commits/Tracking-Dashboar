"""Pure-ASGI middleware: request ids, access timing and security headers."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import internal_error_response
from app.core.logging import request_id_var

logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
# The dashboard UI (served outside /api) may load its own same-origin assets only.
# Inline style attributes are needed by the charting library; scripts stay 'self'.
UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def ui_csp(asset_origin: str | None = None) -> str:
    """The UI policy, plus the course-asset bucket (if configured) for direct uploads,
    image previews and video/audio playback through short-lived signed URLs."""
    if not asset_origin:
        return UI_CSP
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: {asset_origin}; media-src 'self' {asset_origin}; "
        f"connect-src 'self' {asset_origin}; font-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
# Swagger UI loads its own assets; leave CSP off for the docs pages only.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, asset_origin: str | None = None) -> None:
        self.app = app
        self.ui_csp = ui_csp(asset_origin)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = (
            incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        )
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False
        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for key, value in SECURITY_HEADERS.items():
                    if key == "Content-Security-Policy":
                        if path.startswith(_DOCS_PATHS):
                            continue
                        if not path.startswith("/api/"):
                            value = self.ui_csp
                    if key == "Cache-Control" and path.startswith("/assets/"):
                        value = "public, max-age=31536000, immutable"  # content-hashed files
                    headers.setdefault(key, value)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("unhandled error")
            if response_started:
                raise
            await internal_error_response()(scope, receive, send_wrapper)
        finally:
            logger.info(
                "%s %s %s",
                scope.get("method"),
                path,  # path only: query strings may carry sensitive values
                status_code,
                extra={
                    "method": scope.get("method"),
                    "path": path,
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Rejects request bodies above ``max_bytes`` (declared or streamed) with 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self.max_bytes + 1
                if declared > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
        received = 0
        started = False
        rejected = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if rejected:
                return  # we already answered 413; drop whatever the app tries to send
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request" and not rejected:
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    if not started:
                        await self._reject(scope, receive, send)
                    rejected = True
                    # Tell the app the client went away so it stops reading the body.
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, tracking_send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        from fastapi.responses import JSONResponse

        from app.core.errors import error_body

        response = JSONResponse(
            status_code=413,
            content=error_body("request_too_large", f"Request body exceeds {self.max_bytes} bytes"),
        )
        await response(scope, receive, send)
