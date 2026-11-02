"""Course management: taxonomy (class / stream / subject), courses and their content tree
(course -> playlist -> chapter -> lecture -> language variants + resources), uploaded files,
and student enrollments with their history.

Enrollments are the money/access records. They never cascade-delete with a course (the
course row cannot be deleted while it has enrollments) and they keep a snapshot of the
validity and price that applied when the student bought, so later course edits never change
an existing student's access.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utcnow
from app.db.base import Base, TimestampMixin
from app.db.types import JSONText, ScaledDecimal, UTCDateTime
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
    str_enum,
)


class CourseTaxonomyTerm(TimestampMixin, Base):
    """A class (e.g. "Class 12"), stream (e.g. "Commerce") or subject (e.g. "Accountancy")."""

    __tablename__ = "course_taxonomy_terms"
    __table_args__ = (sa.UniqueConstraint("kind", "name_key", name="uq_course_taxonomy_kind_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[TaxonomyKind] = mapped_column(str_enum(TaxonomyKind, "taxonomy_kind"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    name_key: Mapped[str] = mapped_column(sa.String(80), nullable=False)  # case-insensitive
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class CourseAsset(Base):
    """A file in the course-assets bucket. Only metadata and the storage key live here."""

    __tablename__ = "course_assets"
    __table_args__ = (sa.CheckConstraint("size_bytes > 0", name="size_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[CourseAssetKind] = mapped_column(str_enum(CourseAssetKind, "asset_kind"), nullable=False)
    status: Mapped[CourseAssetStatus] = mapped_column(
        str_enum(CourseAssetStatus, "asset_status"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(sa.String(400), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


def _asset_fk() -> Any:
    return sa.ForeignKey("course_assets.id", ondelete="SET NULL")


class Course(TimestampMixin, Base):
    __tablename__ = "courses"
    __table_args__ = (
        sa.CheckConstraint("validity_value > 0", name="validity_positive"),
        sa.CheckConstraint("price IS NULL OR price >= 0", name="price_non_negative"),
        sa.CheckConstraint(
            "original_price IS NULL OR original_price >= 0", name="original_price_non_negative"
        ),
        sa.CheckConstraint(
            "discounted_price IS NULL OR discounted_price >= 0", name="discounted_price_non_negative"
        ),
        sa.Index("ix_courses_status_launch", "status", "launch_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    goals: Mapped[str | None] = mapped_column(sa.Text)
    thumbnail_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    class_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("course_taxonomy_terms.id", ondelete="RESTRICT")
    )
    stream_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("course_taxonomy_terms.id", ondelete="RESTRICT")
    )
    subject_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("course_taxonomy_terms.id", ondelete="RESTRICT")
    )
    is_free: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR")
    price: Mapped[Decimal | None] = mapped_column(ScaledDecimal())  # list price
    original_price: Mapped[Decimal | None] = mapped_column(ScaledDecimal())  # struck-out MRP
    discounted_price: Mapped[Decimal | None] = mapped_column(ScaledDecimal())  # sale price
    status: Mapped[CourseStatus] = mapped_column(str_enum(CourseStatus, "course_status"), nullable=False)
    # When the course opens for purchase / enrollment. Never a student's validity start.
    launch_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Access each student gets, counted from their own successful purchase.
    validity_value: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    validity_unit: Mapped[ValidityUnit] = mapped_column(
        str_enum(ValidityUnit, "validity_unit"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_by: Mapped[CourseActor] = mapped_column(str_enum(CourseActor, "created_by"), nullable=False)
    updated_by: Mapped[CourseActor] = mapped_column(str_enum(CourseActor, "updated_by"), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    playlists: Mapped[list[Playlist]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="Playlist.position",
        passive_deletes=True,
    )

    __mapper_args__ = {"version_id_col": version}


class Playlist(TimestampMixin, Base):
    __tablename__ = "course_playlists"
    __table_args__ = (sa.Index("ix_course_playlists_course_position", "course_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    thumbnail_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    course: Mapped[Course] = relationship(back_populates="playlists")
    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan", order_by="Chapter.position",
        passive_deletes=True,
    )


class Chapter(TimestampMixin, Base):
    __tablename__ = "course_chapters"
    __table_args__ = (sa.Index("ix_course_chapters_playlist_position", "playlist_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_playlists.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    thumbnail_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    playlist: Mapped[Playlist] = relationship(back_populates="chapters")
    lectures: Mapped[list[Lecture]] = relationship(
        back_populates="chapter", cascade="all, delete-orphan", order_by="Lecture.position",
        passive_deletes=True,
    )


class Lecture(TimestampMixin, Base):
    __tablename__ = "course_lectures"
    __table_args__ = (
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"),
        sa.Index("ix_course_lectures_chapter_position", "chapter_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_chapters.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    thumbnail_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    duration_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    is_free_preview: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    status: Mapped[LectureStatus] = mapped_column(str_enum(LectureStatus, "lecture_status"), nullable=False)
    # Language shown first when a student opens the lecture (must have a variant).
    default_language: Mapped[str | None] = mapped_column(sa.String(16))
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    chapter: Mapped[Chapter] = relationship(back_populates="lectures")
    variants: Mapped[list[LectureVariant]] = relationship(
        back_populates="lecture", cascade="all, delete-orphan", order_by="LectureVariant.id",
        passive_deletes=True,
    )
    resources: Mapped[list[LectureResource]] = relationship(
        back_populates="lecture", cascade="all, delete-orphan", order_by="LectureResource.position",
        passive_deletes=True,
    )


class LectureVariant(TimestampMixin, Base):
    """One language version of a lecture (same lecture, different language)."""

    __tablename__ = "course_lecture_variants"
    __table_args__ = (
        sa.UniqueConstraint("lecture_id", "language", name="uq_course_lecture_variants_language"),
        sa.CheckConstraint(
            "video_asset_id IS NULL OR video_url IS NULL", name="one_video_source"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_lectures.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # e.g. "en", "hi", "bho"
    video_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    video_url: Mapped[str | None] = mapped_column(sa.String(1000))  # external https URL
    audio_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    subtitle_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    thumbnail_asset_id: Mapped[int | None] = mapped_column(_asset_fk())
    description: Mapped[str | None] = mapped_column(sa.Text)

    lecture: Mapped[Lecture] = relationship(back_populates="variants")


class LectureResource(TimestampMixin, Base):
    """PDF / notes / study material attached to a lecture."""

    __tablename__ = "course_lecture_resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    lecture_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_lectures.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    asset_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_assets.id", ondelete="RESTRICT"), nullable=False
    )
    is_downloadable: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    lecture: Mapped[Lecture] = relationship(back_populates="resources")


class CourseEnrollment(TimestampMixin, Base):
    """A student's access to a course. Validity and price are snapshots taken at purchase."""

    __tablename__ = "course_enrollments"
    __table_args__ = (
        sa.CheckConstraint("validity_value > 0", name="validity_positive"),
        sa.CheckConstraint("amount_paid >= 0", name="amount_non_negative"),
        sa.CheckConstraint(
            "access_expires_at IS NULL OR access_start_at IS NULL OR access_expires_at > access_start_at",
            name="expiry_after_start",
        ),
        sa.Index("ix_course_enrollments_student", "student_id", "course_id"),
        sa.Index("ix_course_enrollments_course_status", "course_id", "status"),
        sa.Index("ix_course_enrollments_expiry", "status", "access_expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: a course with enrollments can be archived, never deleted.
    course_id: Mapped[int] = mapped_column(sa.ForeignKey("courses.id", ondelete="RESTRICT"), nullable=False)
    course_title: Mapped[str] = mapped_column(sa.String(160), nullable=False)  # at purchase
    student_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)  # app's user id
    student_name: Mapped[str | None] = mapped_column(sa.String(120))
    source: Mapped[EnrollmentSource] = mapped_column(str_enum(EnrollmentSource, "enrollment_source"), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(str_enum(EnrollmentStatus, "enrollment_status"), nullable=False)
    # Idempotency: one enrollment per payment / per order, whatever the number of callbacks.
    payment_provider: Mapped[str | None] = mapped_column(sa.String(40))
    payment_id: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    order_id: Mapped[str | None] = mapped_column(sa.String(128), unique=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    list_price: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)  # price shown
    amount_paid: Mapped[Decimal] = mapped_column(ScaledDecimal(), nullable=False)
    voucher_code: Mapped[str | None] = mapped_column(sa.String(40))
    purchased_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # payment confirmed
    access_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    validity_value: Mapped[int] = mapped_column(sa.Integer, nullable=False)  # snapshot
    validity_unit: Mapped[ValidityUnit] = mapped_column(str_enum(ValidityUnit, "validity_unit"), nullable=False)
    access_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status_reason: Mapped[str | None] = mapped_column(sa.String(255))
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    events: Mapped[list[EnrollmentEvent]] = relationship(
        back_populates="enrollment", order_by="EnrollmentEvent.id", passive_deletes=True
    )

    __mapper_args__ = {"version_id_col": version}


class EnrollmentEvent(Base):
    """Append-only purchase and access history of an enrollment."""

    __tablename__ = "course_enrollment_events"
    __table_args__ = (sa.Index("ix_course_enrollment_events_enrollment", "enrollment_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        sa.ForeignKey("course_enrollments.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    actor: Mapped[CourseActor] = mapped_column(str_enum(CourseActor, "actor"), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONText())
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)

    enrollment: Mapped[CourseEnrollment] = relationship(back_populates="events")
