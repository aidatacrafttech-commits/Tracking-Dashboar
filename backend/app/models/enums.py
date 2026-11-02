from __future__ import annotations

from enum import StrEnum

import sqlalchemy as sa


def str_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """Portable enum column: VARCHAR + CHECK constraint, stores the enum *value*."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class BalanceCapability(StrEnum):
    """What the provider's API can tell us about remaining balance (see Phase 0 matrix)."""

    API = "api"  # actual balance available via API (Deepgram)
    PARTIAL = "partial"  # only part of the balance via API (ElevenLabs plan quota)
    MANUAL_ONLY = "manual_only"  # no balance API (OpenAI, Sarvam)
    POSTPAID = "postpaid"  # billed in arrears; only credits/spend (AWS)


class BalanceKind(StrEnum):
    PREPAID_BALANCE = "prepaid_balance"
    PLAN_QUOTA = "plan_quota"
    PROMOTIONAL_CREDIT = "promotional_credit"


class ValueSource(StrEnum):
    """Where a recorded number came from. Drives how much the UI should trust it."""

    PROVIDER_API = "provider_api"  # actual value reported by the provider
    BILLING_REPORT = "billing_report"  # provider billing data with known lag (AWS CE)
    USAGE_ESTIMATE = "usage_estimate"  # derived: last known balance minus metered usage
    MANUAL = "manual"  # typed in by the owner


class UnitType(StrEnum):
    CURRENCY = "currency"
    CREDITS = "credits"


class PaymentKind(StrEnum):
    TOP_UP = "top_up"
    SUBSCRIPTION = "subscription"
    INVOICE_PAYMENT = "invoice_payment"
    REFUND = "refund"
    OTHER = "other"


class EntryMethod(StrEnum):
    MANUAL = "manual"
    PROVIDER_VERIFIED = "provider_verified"


class AlertState(StrEnum):
    UNKNOWN = "unknown"
    OK = "ok"
    BREACHED = "breached"


class AlertEventType(StrEnum):
    BREACHED = "breached"
    REMINDER = "reminder"
    RECOVERED = "recovered"
    TEST = "test"


class DeliveryStatus(StrEnum):
    PENDING = "pending"  # queued, or waiting for its next retry (next_attempt_at)
    SENDING = "sending"  # claimed by a dispatcher; lease_expires_at guards crashes
    SENT = "sent"
    FAILED = "failed"  # gave up: permanent SMTP error or max attempts reached
    SUPPRESSED = "suppressed"  # recorded but intentionally not emailed (see last_error)


class AttemptOutcome(StrEnum):
    SENT = "sent"
    RETRY = "retry"  # transient failure; another attempt is scheduled
    FAILED = "failed"  # permanent failure or attempts exhausted


class EvaluationStatus(StrEnum):
    OK = "ok"  # fresh, compatible balance above the threshold
    BREACHED = "breached"
    NO_DATA = "no_data"  # no eligible balance snapshot: unknown, never treated as zero
    STALE = "stale"  # newest eligible snapshot is too old to act on
    CURRENCY_MISMATCH = "currency_mismatch"
    INCOMPATIBLE_UNITS = "incompatible_units"  # e.g. credits vs money
    DISABLED = "disabled"


class RunTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    STARTUP = "startup"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class HealthSource(StrEnum):
    STATUS_PAGE = "status_page"
    API_PROBE = "api_probe"


class HealthStatus(StrEnum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    PARTIAL_OUTAGE = "partial_outage"
    MAJOR_OUTAGE = "major_outage"
    MAINTENANCE = "maintenance"
    UNKNOWN = "unknown"


class Actor(StrEnum):
    OWNER = "owner"
    VIEWER = "viewer"
    SYSTEM = "system"


class VoucherActor(StrEnum):
    """Who changed a voucher. The dashboard has one owner identity; integrations use their
    own token, so "voucher_api" distinguishes calls made by the app from the owner's."""

    OWNER = "owner"
    VOUCHER_API = "voucher_api"
    SYSTEM = "system"


class VoucherEventOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class VoucherEventSource(StrEnum):
    DASHBOARD = "dashboard"
    API = "api"


class VoucherCategory(StrEnum):
    """What a voucher can be redeemed against."""

    COURSE = "course"
    LIVE_TUTOR = "live_tutor"


class VoucherDiscountType(StrEnum):
    PERCENTAGE = "percentage"
    FIXED = "fixed"


class TutorPackage(StrEnum):
    """Live Tutor packages a voucher can apply to."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    PRO = "pro"


class CourseStatus(StrEnum):
    """Stored status. "Scheduled" is derived: published with a future launch date."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class TaxonomyKind(StrEnum):
    CLASS = "class"
    STREAM = "stream"
    SUBJECT = "subject"


class ValidityUnit(StrEnum):
    DAYS = "days"
    MONTHS = "months"
    YEARS = "years"


class LectureStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class CourseAssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    DOCUMENT = "document"


class CourseAssetStatus(StrEnum):
    PENDING = "pending"
    UPLOADED = "uploaded"


class EnrollmentStatus(StrEnum):
    PENDING = "pending"  # order created, payment not confirmed yet
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class EnrollmentSource(StrEnum):
    PAYMENT = "payment"
    FREE = "free"


class CourseActor(StrEnum):
    OWNER = "owner"
    COURSE_API = "course_api"  # the trusted app backend
    SYSTEM = "system"  # automatic (e.g. expiry)
