"""SMTP delivery (stdlib smtplib) with safe error classification.

Errors carry a short code and a redacted message; SMTP passwords never leave this module.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from app.core.config import Settings

SmtpFactory = Callable[..., Any]


@dataclass
class DeliveryError(Exception):
    code: str
    message: str
    retryable: bool
    smtp_status: int | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def classify_smtp_error(exc: BaseException) -> DeliveryError:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        # A config problem; bounded retries give the owner time to fix credentials.
        return DeliveryError(
            "auth_failed",
            "SMTP authentication failed; check SMTP_USERNAME/SMTP_PASSWORD",
            retryable=True,
            smtp_status=exc.smtp_code,
        )
    # SMTP reply codes decide retryability: 4xx is temporary whichever command it answers.
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        temporary = bool(codes) and all(400 <= code < 500 for code in codes)
        return DeliveryError(
            "temporary_failure" if temporary else "recipients_refused",
            f"the SMTP server refused all recipients ({', '.join(map(str, sorted(set(codes))))})",
            retryable=temporary,
            smtp_status=codes[0] if codes else None,
        )
    if isinstance(exc, smtplib.SMTPSenderRefused):
        temporary = 400 <= exc.smtp_code < 500
        return DeliveryError(
            "temporary_failure" if temporary else "sender_refused",
            f"sender address refused ({exc.smtp_code})",
            retryable=temporary,
            smtp_status=exc.smtp_code,
        )
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return DeliveryError(
            "tls_unavailable",
            "server does not support the configured SMTP_SECURITY mode",
            retryable=False,
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        temporary = 400 <= exc.smtp_code < 500
        return DeliveryError(
            "temporary_failure" if temporary else "rejected",
            f"SMTP server replied {exc.smtp_code}",  # never echo upstream text
            retryable=temporary,
            smtp_status=exc.smtp_code,
        )
    if isinstance(exc, smtplib.SMTPServerDisconnected | smtplib.SMTPConnectError):
        return DeliveryError(
            "unavailable", "SMTP server disconnected or refused the connection", retryable=True
        )
    if isinstance(exc, ssl.SSLError):
        return DeliveryError("tls_error", f"TLS error ({type(exc).__name__})", retryable=True)
    if isinstance(exc, TimeoutError):
        return DeliveryError("timeout", "SMTP connection timed out", retryable=True)
    if isinstance(exc, OSError):
        return DeliveryError(
            "unavailable", f"cannot reach SMTP server ({type(exc).__name__})", retryable=True
        )
    if isinstance(exc, smtplib.SMTPException):
        return DeliveryError("smtp_error", f"SMTP error ({type(exc).__name__})", retryable=True)
    return DeliveryError("unexpected", f"unexpected error ({type(exc).__name__})", retryable=True)


class SmtpSender:
    def __init__(
        self,
        settings: Settings,
        *,
        smtp_factory: SmtpFactory | None = None,
        smtp_ssl_factory: SmtpFactory | None = None,
    ) -> None:
        self.settings = settings
        self._smtp = smtp_factory or smtplib.SMTP
        self._smtp_ssl = smtp_ssl_factory or smtplib.SMTP_SSL
        self._ssl_context: ssl.SSLContext | None = None

    def _tls_context(self) -> ssl.SSLContext:
        # Certificate-verifying context, built once (loading the trust store is slow).
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    @property
    def configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    def send(self, message: EmailMessage) -> None:
        if not self.configured:
            raise DeliveryError(
                "not_configured", "SMTP_HOST and SMTP_FROM are not set", retryable=False
            )
        s = self.settings
        context = self._tls_context() if s.smtp_security != "none" else None
        try:
            if s.smtp_security == "ssl":
                client = self._smtp_ssl(
                    s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=context
                )
            else:
                client = self._smtp(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
            with client:
                client.ehlo()
                if s.smtp_security == "starttls":
                    client.starttls(context=context)
                    client.ehlo()
                if s.smtp_username and s.smtp_password:
                    client.login(s.smtp_username, s.smtp_password.get_secret_value())
                refused = client.send_message(message)
                if refused:
                    # Some recipients accepted; report which were refused (addresses only).
                    raise DeliveryError(
                        "partially_refused",
                        "refused: " + ", ".join(sorted(refused))[:200],
                        retryable=False,
                    )
        except DeliveryError:
            raise
        except BaseException as exc:  # noqa: BLE001 - everything is classified, nothing leaks
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            raise classify_smtp_error(exc) from None
