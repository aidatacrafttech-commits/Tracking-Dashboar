"""Course management API models (admin dashboard + the trusted app backend)."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.models.enums import (
    CourseActor,
    CourseAssetKind,
    CourseAssetStatus,
    CourseStatus,
    EnrollmentSource,
    EnrollmentStatus,
    LectureStatus,
    TaxonomyKind,
    ValidityUnit,
)
from app.schemas.common import ApiModel, DecimalStr, InputModel, UTCInput, normalize_currency
from app.schemas.voucher import EXTERNAL_ID_RE, REFERENCE_RE, parse_amount
from app.services.course_validity import MAX_VALIDITY

# Language variants a lecture can have (code -> display name).
LANGUAGES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "bho": "Bhojpuri",
    "mr": "Marathi",
    "ta": "Tamil",
    "te": "Telugu",
    "bn": "Bengali",
    "gu": "Gujarati",
    "kn": "Kannada",
    "ml": "Malayalam",
    "pa": "Punjabi",
    "or": "Odia",
    "ur": "Urdu",
    "as": "Assamese",
}
DisplayStatus = Literal["draft", "scheduled", "published", "archived"]
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if CONTROL_RE.search(value):
        raise ValueError("contains control characters")
    return value or None


def _money(value: Any) -> Decimal | None:
    return None if value is None or value == "" else parse_amount(value)


# --- taxonomy ------------------------------------------------------------------------------


class TaxonomyCreate(InputModel):
    kind: TaxonomyKind
    name: str = Field(min_length=1, max_length=80)
    sort_order: int = Field(default=0, ge=0, le=10_000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = _clean(" ".join(value.split()))
        if not cleaned:
            raise ValueError("enter a name")
        return cleaned


class TaxonomyUpdate(InputModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _clean(" ".join(value.split())) if value is not None else None


class TaxonomyRead(ApiModel):
    id: int
    kind: TaxonomyKind
    name: str
    sort_order: int
    is_active: bool
    course_count: int = 0


# --- courses ---------------------------------------------------------------------------------


class CourseFields(InputModel):
    title: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=10_000)
    goals: str | None = Field(default=None, max_length=5_000)
    thumbnail_asset_id: int | None = Field(default=None, ge=1)
    class_id: int | None = Field(default=None, ge=1)
    stream_id: int | None = Field(default=None, ge=1)
    subject_id: int | None = Field(default=None, ge=1)
    is_free: bool = False
    currency: str = Field(default="INR", min_length=3, max_length=3)
    price: Decimal | None = None
    original_price: Decimal | None = None
    discounted_price: Decimal | None = None
    launch_at: UTCInput | None = None
    validity_value: int = Field(ge=1, le=3650)
    validity_unit: ValidityUnit

    @field_validator("title", "description", "goals")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("price", "original_price", "discounted_price", mode="before")
    @classmethod
    def _amounts(cls, value: Any) -> Decimal | None:
        return _money(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return normalize_currency(value)

    @model_validator(mode="after")
    def _rules(self) -> CourseFields:
        if not self.title or len(self.title) < 2:
            raise ValueError("title must be at least 2 characters")
        if self.validity_value > MAX_VALIDITY[self.validity_unit]:
            raise ValueError(
                f"validity can be at most {MAX_VALIDITY[self.validity_unit]}"
                f" {self.validity_unit.value}"
            )
        if self.is_free:
            if any(p for p in (self.price, self.original_price, self.discounted_price)):
                raise ValueError("a free course has no price; clear the price fields")
            self.price = self.original_price = self.discounted_price = None
            return self
        if self.price is None or self.price <= 0:
            raise ValueError("a paid course needs a price above 0")
        if self.discounted_price is not None and not 0 < self.discounted_price < self.price:
            raise ValueError("the discounted price must be above 0 and below the course price")
        if self.original_price is not None and self.original_price < self.price:
            raise ValueError("the original price (MRP) cannot be below the course price")
        return self


class CourseCreate(CourseFields):
    pass


class CourseUpdate(CourseFields):
    version: int = Field(ge=1)


class AssetRef(ApiModel):
    id: int
    kind: CourseAssetKind
    status: CourseAssetStatus
    filename: str
    content_type: str
    size_bytes: int
    url: str | None = None  # short-lived signed URL (dashboard preview)


class CourseRead(ApiModel):
    id: int
    title: str
    description: str | None
    goals: str | None
    thumbnail: AssetRef | None
    class_id: int | None
    class_name: str | None
    stream_id: int | None
    stream_name: str | None
    subject_id: int | None
    subject_name: str | None
    is_free: bool
    currency: str
    price: DecimalStr | None
    original_price: DecimalStr | None
    discounted_price: DecimalStr | None
    effective_price: DecimalStr  # what a student pays before any voucher (0 if free)
    status: CourseStatus
    display_status: DisplayStatus
    launch_at: datetime | None
    validity_value: int
    validity_unit: ValidityUnit
    validity_label: str
    published_at: datetime | None
    archived_at: datetime | None
    playlist_count: int
    lecture_count: int
    enrollment_count: int
    active_enrollment_count: int
    created_at: datetime
    created_by: CourseActor
    updated_at: datetime
    updated_by: CourseActor
    version: int
    can_delete: bool


class CoursePage(ApiModel):
    items: list[CourseRead]
    total: int
    limit: int
    offset: int


class VariantRead(ApiModel):
    id: int
    language: str
    language_name: str
    video: AssetRef | None
    video_url: str | None
    audio: AssetRef | None
    subtitle: AssetRef | None
    thumbnail: AssetRef | None
    description: str | None


class ResourceRead(ApiModel):
    id: int
    title: str
    asset: AssetRef
    is_downloadable: bool
    position: int


class LectureRead(ApiModel):
    id: int
    title: str
    description: str | None
    thumbnail: AssetRef | None
    duration_seconds: int | None
    is_free_preview: bool
    status: LectureStatus
    default_language: str | None
    position: int
    variants: list[VariantRead]
    resources: list[ResourceRead]


class ChapterRead(ApiModel):
    id: int
    title: str
    description: str | None
    thumbnail: AssetRef | None
    position: int
    lectures: list[LectureRead]


class PlaylistRead(ApiModel):
    id: int
    title: str
    description: str | None
    thumbnail: AssetRef | None
    position: int
    chapters: list[ChapterRead]


class CourseDetail(CourseRead):
    playlists: list[PlaylistRead]


class PublishProblem(ApiModel):
    code: str
    message: str
    blocking: bool


class PublishCheck(ApiModel):
    ready: bool
    problems: list[PublishProblem]


# --- content -----------------------------------------------------------------------------------


class _Named(InputModel):
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=5_000)
    thumbnail_asset_id: int | None = Field(default=None, ge=1)

    @field_validator("title", "description")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _clean(value)

    @model_validator(mode="after")
    def _title(self) -> _Named:
        if not self.title:
            raise ValueError("enter a title")
        return self


class PlaylistInput(_Named):
    pass


class ChapterInput(_Named):
    pass


class LectureInput(_Named):
    duration_seconds: int | None = Field(default=None, ge=0, le=86_400)
    is_free_preview: bool = False
    status: LectureStatus = LectureStatus.DRAFT
    default_language: str | None = None

    @field_validator("default_language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is not None and value not in LANGUAGES:
            raise ValueError("unknown language")
        return value


class VariantInput(InputModel):
    language: str
    video_asset_id: int | None = Field(default=None, ge=1)
    video_url: str | None = Field(default=None, max_length=1000)
    audio_asset_id: int | None = Field(default=None, ge=1)
    subtitle_asset_id: int | None = Field(default=None, ge=1)
    thumbnail_asset_id: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=5_000)

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError(f"choose one of: {', '.join(LANGUAGES)}")
        return value

    @field_validator("video_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        value = _clean(value)
        if value is not None and not re.match(r"^https://[^\s/$.?#][^\s]*$", value):
            raise ValueError("must be an https:// URL")
        return value

    @model_validator(mode="after")
    def _one_video(self) -> VariantInput:
        if self.video_asset_id is not None and self.video_url is not None:
            raise ValueError("use either an uploaded video or a video URL, not both")
        return self


class ResourceInput(InputModel):
    title: str = Field(min_length=1, max_length=160)
    asset_id: int = Field(ge=1)
    is_downloadable: bool = True


class OrderInput(InputModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


# --- assets ------------------------------------------------------------------------------------


class UploadRequest(InputModel):
    kind: CourseAssetKind
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=3, max_length=100)
    size_bytes: int = Field(ge=1)


class UploadTicket(ApiModel):
    asset: AssetRef
    upload_url: str
    fields: dict[str, str]  # form fields to POST with the file (S3 presigned POST)
    expires_in: int


class StorageStatus(ApiModel):
    configured: bool
    max_video_mb: int
    max_file_mb: int
    allowed_types: dict[str, list[str]]


# --- enrollments -----------------------------------------------------------------------------


class EnrollmentRead(ApiModel):
    id: int
    course_id: int
    course_title: str
    student_id: str
    student_name: str | None
    source: EnrollmentSource
    status: EnrollmentStatus
    payment_provider: str | None
    payment_id: str | None
    order_id: str | None
    currency: str
    list_price: DecimalStr
    amount_paid: DecimalStr
    voucher_code: str | None
    purchased_at: datetime | None
    access_start_at: datetime | None
    validity_value: int
    validity_unit: ValidityUnit
    validity_label: str
    access_expires_at: datetime | None
    status_reason: str | None
    created_at: datetime
    updated_at: datetime


class EnrollmentEventRead(ApiModel):
    id: int
    event: str
    actor: CourseActor
    details: dict[str, Any] | None
    occurred_at: datetime


class EnrollmentDetail(EnrollmentRead):
    events: list[EnrollmentEventRead]


class EnrollmentPage(ApiModel):
    items: list[EnrollmentRead]
    total: int
    limit: int
    offset: int


class CancelRequest(InputModel):
    reason: str = Field(min_length=3, max_length=255)


class MoneyTotal(ApiModel):
    currency: str
    amount: DecimalStr


class CourseSummary(ApiModel):
    total_courses: int
    published: int
    scheduled: int
    draft: int
    archived: int
    total_enrollments: int
    active_enrollments: int
    expired_enrollments: int
    pending_enrollments: int
    revenue: list[MoneyTotal]  # paid, not refunded or cancelled


# --- trusted app backend (integration) ---------------------------------------------------------


class _Student(InputModel):
    student_id: str = Field(min_length=1, max_length=128)
    student_name: str | None = Field(default=None, max_length=120)

    @field_validator("student_id")
    @classmethod
    def _student(cls, value: str) -> str:
        if not EXTERNAL_ID_RE.match(value):
            raise ValueError("letters, digits and . _ : @ - only (max 128)")
        return value

    @field_validator("student_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _clean(value)


def _reference(value: str | None) -> str | None:
    if value is not None and not REFERENCE_RE.match(value):
        raise ValueError("letters, digits and . _ : / # @ - only (max 128)")
    return value


class OrderCreate(_Student):
    """A checkout started in the app: records a pending enrollment (payment not confirmed)."""

    course_id: int = Field(ge=1)
    order_id: str = Field(min_length=1, max_length=128)
    payment_provider: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_.-]+$")

    @field_validator("order_id")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return _reference(value)


class PaymentConfirm(_Student):
    """Sent by the app backend after IT has verified the payment with the provider."""

    course_id: int = Field(ge=1)
    payment_id: str = Field(min_length=1, max_length=128)
    order_id: str | None = Field(default=None, max_length=128)
    payment_provider: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_.-]+$")
    amount_paid: Decimal
    currency: str = Field(default="INR", min_length=3, max_length=3)
    paid_at: UTCInput | None = None
    voucher_code: str | None = Field(default=None, max_length=40)

    @field_validator("payment_id", "order_id")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        return _reference(value)

    @field_validator("amount_paid", mode="before")
    @classmethod
    def _amount(cls, value: Any) -> Decimal:
        return parse_amount(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return normalize_currency(value)

    @field_validator("voucher_code")
    @classmethod
    def _voucher(cls, value: str | None) -> str | None:
        return value.strip().upper() or None if value else None


class FreeEnroll(_Student):
    course_id: int = Field(ge=1)


class PaymentReversal(InputModel):
    payment_id: str = Field(min_length=1, max_length=128)
    status: Literal["refunded", "cancelled"]
    reason: str = Field(min_length=3, max_length=255)


class Availability(ApiModel):
    purchasable: bool
    reason: Literal["not_launched", "archived", "draft"] | None
    launch_at: datetime | None


class AccessState(ApiModel):
    status: Literal["active", "pending", "expired", "cancelled", "refunded", "none"]
    access_expires_at: datetime | None
    enrollment_id: int | None
    message: str


class StudentLecture(ApiModel):
    id: int
    title: str
    duration_seconds: int | None
    is_free_preview: bool
    languages: list[str]
    resource_count: int


class StudentChapter(ApiModel):
    id: int
    title: str
    lectures: list[StudentLecture]


class StudentPlaylist(ApiModel):
    id: int
    title: str
    description: str | None
    chapters: list[StudentChapter]


class StudentCourse(ApiModel):
    id: int
    title: str
    description: str | None
    goals: str | None
    thumbnail_url: str | None
    class_name: str | None
    stream_name: str | None
    subject_name: str | None
    is_free: bool
    currency: str
    price: DecimalStr | None
    original_price: DecimalStr | None
    effective_price: DecimalStr
    validity_value: int
    validity_unit: ValidityUnit
    validity_label: str
    availability: Availability
    access: AccessState | None = None  # when a student_id is given


class StudentCourseDetail(StudentCourse):
    playlists: list[StudentPlaylist]


class StudentCatalog(ApiModel):
    items: list[StudentCourse]


class PlaybackResource(ApiModel):
    id: int
    title: str
    is_downloadable: bool


class Playback(ApiModel):
    lecture_id: int
    title: str
    language: str
    language_name: str
    available_languages: list[dict[str, str]]
    video_url: str
    audio_url: str | None
    subtitle_url: str | None
    url_expires_in: int | None  # seconds; None for external URLs
    free_preview: bool
    access_expires_at: datetime | None
    resources: list[PlaybackResource]


class DownloadLink(ApiModel):
    url: str
    expires_in: int


class EnrollmentResult(ApiModel):
    enrollment: EnrollmentRead
    duplicate: bool  # True when this payment had already been processed
