"""Structured logging with secret redaction.

Redaction is applied at format time to the final output, so it also covers
exception tracebacks, uvicorn access logs and anything passed via ``extra=``.
Three layers:
  1. Exact secret values registered at startup (tokens, provider keys).
  2. Values of sensitive-looking keys in structured fields.
  3. Regex patterns for well-known credential formats.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

SENSITIVE_KEY_RE = re.compile(
    r"(authorization|api[-_]?key|secret|token|password|passwd|cookie|credential|"
    r"private[-_]?key|signature)",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization header schemes (OpenAI/ElevenLabs Bearer, Deepgram "Token").
    # 20+ chars so ordinary prose like "token expired" is left alone.
    (re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=-]{20,}"), rf"\1 {REDACTED}"),
    # OpenAI-style keys (sk-..., sk-proj-..., sk-admin-...).
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    # AWS access key ids.
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # JWTs.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # key=value / "key": "value" pairs with sensitive key names.
    (
        re.compile(
            r"(?i)([\"']?[\w-]*(?:api[-_]?key|secret|token|password|passwd)[\w-]*[\"']?"
            r"\s*[:=]\s*[\"']?)([^\s\"'&,}]+)"
        ),
        rf"\1{REDACTED}",
    ),
]

_registered_secrets: set[str] = set()
_secrets_lock = threading.Lock()

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}  # color_message: uvicorn's ANSI duplicate of msg
)


def register_secrets(*values: str | None) -> None:
    """Register literal secret values so they are scrubbed from every log line."""
    with _secrets_lock:
        for value in values:
            if value and len(value) >= 8:
                _registered_secrets.add(value)


def redact_text(text: str) -> str:
    with _secrets_lock:
        secrets = sorted(_registered_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, key: str | None = None) -> Any:
    if key is not None and SENSITIVE_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        payload = redact_value(payload)
        return redact_text(json.dumps(payload, default=str, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get() or "-"
        return redact_text(super().format(record))


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's loggers through our redacting handler.
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # Requests are logged once, by the app's own access middleware (with request ID and
    # status). uvicorn's access logger would duplicate every line and ignore --no-access-log.
    uv_access = logging.getLogger("uvicorn.access")
    uv_access.handlers.clear()
    uv_access.propagate = False
    # SQL echo would log bound parameters; keep it quiet unless debugging locally.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # Request URLs can carry credentials in query strings; never log them at INFO.
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
