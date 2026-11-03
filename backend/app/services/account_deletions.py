"""Server-side client for the AdhyaapakAI account-deletion admin API.

The browser calls this dashboard; this module calls AdhyaapakAI with the `x-admin-secret`
header taken from ADHYAAPAK_ADMIN_SECRET, so the secret never reaches a browser bundle and
no CORS change is needed on the AdhyaapakAI side (this is a server-to-server call).

Responses contain student emails and names: they are passed to the dashboard UI but never
logged. Logs carry the method, path and status code only.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import AppError, ServiceUnavailableError

logger = logging.getLogger(__name__)

LIST_PATH = "/api/admin/accounts/deletions"
REACTIVATE_PATH = "/api/admin/accounts/reactivate"
PURGE_PATH = "/api/admin/accounts/purge-now"
PASS_THROUGH = (400, 404, 409, 410)


class UpstreamError(AppError):
    """An AdhyaapakAI answer the dashboard reports to the admin (detail shown as-is)."""

    def __init__(
        self, status_code: int, message: str, *, code: str, details: Any | None = None
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.status_code = status_code


def mask_email(email: str) -> str:
    """For the dashboard's own audit log: enough to recognise, not the full address."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


class AdhyaapakAdminClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.adhyaapak_api_base_url or settings.adhyaapak_admin_secret is None:
            raise ServiceUnavailableError(
                "Account deletions are not configured: set ADHYAAPAK_API_BASE_URL and "
                "ADHYAAPAK_ADMIN_SECRET in the dashboard's backend/.env and restart it",
                code="adhyaapak_not_configured",
            )
        self._base = settings.adhyaapak_api_base_url.rstrip("/")
        self._secret = settings.adhyaapak_admin_secret.get_secret_value()
        self._timeout = settings.http_timeout_seconds
        self._transport = transport

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self._base,
                timeout=self._timeout,
                transport=self._transport,
                headers={"x-admin-secret": self._secret, "Accept": "application/json"},
                follow_redirects=False,
            ) as client:
                response = client.request(method, path, json=body)
        except httpx.TimeoutException as exc:
            logger.warning("adhyaapak admin call timed out", extra={"method": method, "path": path})
            raise UpstreamError(
                504,
                "The AdhyaapakAI backend did not answer in time. Try again.",
                code="adhyaapak_timeout",
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "adhyaapak admin call failed",
                extra={"method": method, "path": path, "error_type": type(exc).__name__},
            )
            raise UpstreamError(
                502, "The AdhyaapakAI backend could not be reached.", code="adhyaapak_unreachable"
            ) from exc
        # Method, path and status only: bodies contain student emails.
        logger.info(
            "adhyaapak admin call",
            extra={"method": method, "path": path, "status": response.status_code},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_success:
            if not isinstance(payload, dict):
                raise UpstreamError(
                    502,
                    "The AdhyaapakAI backend sent an unreadable answer.",
                    code="adhyaapak_bad_response",
                )
            return payload
        detail = payload.get("detail") if isinstance(payload, dict) else None
        upstream_code = payload.get("code") if isinstance(payload, dict) else None
        status = response.status_code
        if status in (401, 403):
            raise UpstreamError(
                502,
                "Not authorised: the AdhyaapakAI backend rejected the dashboard's admin secret "
                "(ADHYAAPAK_ADMIN_SECRET in backend/.env).",
                code="adhyaapak_forbidden",
            )
        message = (
            detail if isinstance(detail, str) else f"The AdhyaapakAI backend answered {status}."
        )
        code = str(upstream_code).lower() if upstream_code else f"adhyaapak_{status}"
        if status in PASS_THROUGH:
            raise UpstreamError(status, message, code=code)
        if status == 422:
            raise UpstreamError(
                422,
                "The AdhyaapakAI backend rejected the request.",
                code="adhyaapak_invalid",
                details=detail if isinstance(detail, list) else None,
            )
        # 5xx (e.g. a purge that failed part-way; its sweeper retries) and anything else.
        raise UpstreamError(502, message, code=code if upstream_code else "adhyaapak_error")

    def list_deletions(self) -> dict[str, Any]:
        return self._request("GET", LIST_PATH)

    def reactivate(self, email: str, note: str) -> dict[str, Any]:
        return self._request("POST", REACTIVATE_PATH, {"email": email, "note": note})

    def purge_now(self, email: str, note: str) -> dict[str, Any]:
        # `force` is never sent: purging an account that never asked is not offered here.
        return self._request("POST", PURGE_PATH, {"email": email, "note": note, "force": False})
