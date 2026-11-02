"""Centralized error types and handlers.

Every error response has the same envelope:
    {"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}
Unexpected exceptions are logged with a traceback but never echoed to the client.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self, message: str, *, code: str | None = None, details: Any | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class DomainValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def error_body(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details is not None:
        body["details"] = details
    return {"error": body}


def _json(status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
    return JSONResponse(status_code=status_code, content=body, headers=headers)


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    headers: dict[str, str] | None = None
    if isinstance(exc, AuthenticationError):
        headers = {"WWW-Authenticate": "Bearer"}
    elif isinstance(exc, RateLimitedError) and isinstance(exc.details, dict):
        retry = exc.details.get("retry_after_seconds")
        if retry is not None:
            headers = {"Retry-After": str(retry)}
    if exc.status_code >= 500:
        logger.error("request failed: %s", exc.code, extra={"error_code": exc.code})
    return _json(exc.status_code, error_body(exc.code, exc.message, exc.details), headers)


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Deliberately omit the submitted "input" values: they may contain sensitive data.
    details = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": str(err.get("msg", "")).removeprefix("Value error, "),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return _json(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        error_body("validation_error", "Request validation failed", details),
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        401: "unauthenticated",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
    }.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return _json(exc.status_code, error_body(code, message), getattr(exc, "headers", None))


async def _integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    # Driver messages can include SQL and values; log the type only.
    logger.warning("integrity error", extra={"error_type": type(exc.orig).__name__})
    return _json(
        status.HTTP_409_CONFLICT,
        error_body("conflict", "The request conflicts with existing data"),
    )


async def _operational_handler(request: Request, exc: OperationalError) -> JSONResponse:
    text = str(exc.orig).lower()
    if "disk is full" in text or "database or disk is full" in text:
        logger.critical("database storage is full; writes are failing")
        return _json(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            error_body("storage_full", "The server has run out of storage; data cannot be saved"),
        )
    logger.error("database unavailable", extra={"error_type": type(exc.orig).__name__})
    return _json(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        error_body("database_unavailable", "Database is temporarily unavailable"),
    )


def internal_error_response() -> JSONResponse:
    """Used by RequestContextMiddleware for unexpected exceptions.

    Handled there rather than via ``add_exception_handler(Exception, ...)`` because
    Starlette runs that handler outside user middleware, after the request id is gone.
    """
    return _json(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_body("internal_error", "An unexpected error occurred"),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, _integrity_handler)  # type: ignore[arg-type]
    app.add_exception_handler(OperationalError, _operational_handler)  # type: ignore[arg-type]
