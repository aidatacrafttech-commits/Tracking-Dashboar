from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.models.enums import (
    AlertEventType,
    AlertState,
    AttemptOutcome,
    DeliveryStatus,
    EvaluationStatus,
)
from app.schemas.common import EMAIL_PATTERN, ApiModel, DecimalStr, InputModel


class EvaluationRead(ApiModel):
    provider: str
    status: EvaluationStatus
    message: str
    alert_state: AlertState
    balance: DecimalStr | None
    unit: str | None
    snapshot_id: int | None
    events_created: list[int]
    skipped_concurrent: bool


class DeliveryAttemptRead(ApiModel):
    attempt_number: int
    outcome: AttemptOutcome
    error_code: str | None
    error_message: str | None
    smtp_status: int | None
    started_at: datetime
    finished_at: datetime


class AlertEventRead(ApiModel):
    id: int
    provider: str
    event_type: AlertEventType
    episode: int | None
    observed_amount: DecimalStr | None
    threshold_amount: DecimalStr
    unit: str
    recipients: list[str] | None
    delivery_status: DeliveryStatus
    delivery_attempts: int
    last_error: str | None
    subject: str | None
    dedupe_key: str
    occurred_at: datetime
    next_attempt_at: datetime | None
    sent_at: datetime | None
    # Balance source that triggered the alert (api_verified / manual / usage_estimate).
    source_type: str | None = None
    attempts: list[DeliveryAttemptRead] = []


class AlertEventDetail(AlertEventRead):
    body_text: str | None
    body_html: str | None
    context: dict[str, Any] | None


class AlertPage(ApiModel):
    items: list[AlertEventRead]
    total: int
    limit: int
    offset: int


class EmailPreviewRead(ApiModel):
    provider: str
    event_type: AlertEventType
    subject: str
    text: str
    html: str
    recipients: list[str]
    email_delivery_enabled: bool
    note: str


class TestEmailRequest(InputModel):
    to: str | None = Field(default=None, description="Defaults to ALERT_RECIPIENTS")

    @field_validator("to")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if len(value) > 254 or not EMAIL_PATTERN.match(value):
            raise ValueError("invalid email address")
        return value


class TestEmailRead(ApiModel):
    event_id: int
    recipients: list[str]
    delivery_status: DeliveryStatus | None
    error_code: str | None
    error_message: str | None


class NotificationStatusRead(ApiModel):
    email_delivery_enabled: bool
    smtp_configured: bool
    smtp_host: str | None
    smtp_port: int
    smtp_security: Literal["starttls", "ssl", "none"]
    smtp_from: str | None
    smtp_auth_configured: bool  # whether credentials are set; values are never returned
    default_recipients: list[str]
    dashboard_url: str | None
    exchange_rates_configured: bool
    exchange_rate_source: str | None
    outbox: dict[str, int]
