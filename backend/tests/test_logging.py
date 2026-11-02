from __future__ import annotations

import json
import logging
import sys

from app.core.logging import REDACTED, JsonFormatter, TextFormatter, redact_text, register_secrets

# Fake credential shapes only; none of these are real keys.
FAKE_OPENAI = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_AWS_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl"


def _record(msg: str, *args: object, exc: bool = False, **extra: object) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise ValueError(f"failed with key {FAKE_OPENAI}")
        except ValueError:
            exc_info = sys.exc_info()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_known_credential_patterns_are_redacted() -> None:
    text = (
        f"Authorization: Bearer {FAKE_OPENAI} aws={FAKE_AWS_ID} jwt {FAKE_JWT} "
        "Token 0123456789abcdef0123456789abcdef api_key=plainvalue password: hunter2"
    )
    out = redact_text(text)
    for leaked in (
        FAKE_OPENAI,
        FAKE_AWS_ID,
        FAKE_JWT,
        "0123456789abcdef0123456789abcdef",
        "plainvalue",
        "hunter2",
    ):
        assert leaked not in out
    assert out.count(REDACTED) >= 5


def test_ordinary_text_is_untouched() -> None:
    text = "token expired for provider deepgram after 3 retries"
    assert redact_text(text) == text


def test_registered_secret_values_are_redacted() -> None:
    secret = "custom-secret-without-known-shape-42"
    register_secrets(secret)
    assert secret not in redact_text(f"value={secret!r} in sentence {secret}")


def test_json_formatter_redacts_args_extra_and_exceptions() -> None:
    record = _record(
        "calling %s",
        f"https://api.example.com?api_key={FAKE_OPENAI}",
        exc=True,
        headers={"xi-api-key": "abc123def456", "Accept": "application/json"},
        api_key="should-not-appear",
    )
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert FAKE_OPENAI not in line
    assert "abc123def456" not in line
    assert "should-not-appear" not in line
    assert payload["headers"]["Accept"] == "application/json"
    assert payload["api_key"] == REDACTED
    assert payload["level"] == "INFO"
    assert payload["ts"].endswith("+00:00")


def test_text_formatter_redacts() -> None:
    line = TextFormatter().format(_record("key %s", FAKE_OPENAI))
    assert FAKE_OPENAI not in line and REDACTED in line


def test_each_request_is_logged_once(capsys) -> None:  # type: ignore[no-untyped-def]
    """uvicorn's access logger must not duplicate the app's own access line (the audit found
    every request logged twice, despite --no-access-log in the systemd unit)."""
    from app.core.logging import configure_logging

    configure_logging("INFO", json_logs=True)
    logging.getLogger("uvicorn.access").info('127.0.0.1:0 - "GET /x HTTP/1.1" 200')
    logging.getLogger("app.access").info("GET /x 200")
    logging.getLogger("uvicorn.error").info("server started")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [entry["logger"] for entry in lines] == ["app.access", "uvicorn.error"]
