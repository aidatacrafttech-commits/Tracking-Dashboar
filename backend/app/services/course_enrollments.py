"""Enrollments, purchases and student access.

The most important rule: a student's access starts at THEIR OWN successful purchase and
lasts the validity that applied when they bought. Each enrollment stores that snapshot
(validity value + unit, list price, amount paid) and its computed expiry; nothing ever
recalculates an existing enrollment from the course's current settings.

Payments are verified by the trusted app backend (the one integrated with the payment
provider). It calls this service with the verified payment; `payment_id` (and `order_id`)
are unique, so repeated callbacks return the same enrollment instead of creating another.

Expiry is enforced on every access check by comparing `access_expires_at` with the clock;
`expire_due` additionally records the transition (status + history) for reporting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from app.core.time import utcnow
from app.db.session import transaction
from app.models import (
    Course,
    CourseAsset,
    CourseEnrollment,
    CourseTaxonomyTerm,
    EnrollmentEvent,
    Lecture,
    LectureResource,
)
from app.models.enums import (
    CourseActor,
    CourseAssetStatus,
    CourseStatus,
    EnrollmentSource,
    EnrollmentStatus,
    LectureStatus,
)
from app.repositories.audit import AuditLogRepository
from app.schemas.course import (
    LANGUAGES,
    AccessState,
    Availability,
    CourseSummary,
    DownloadLink,
    EnrollmentDetail,
    EnrollmentEventRead,
    EnrollmentPage,
    EnrollmentRead,
    EnrollmentResult,
    FreeEnroll,
    MoneyTotal,
    OrderCreate,
    PaymentConfirm,
    PaymentReversal,
    Playback,
    PlaybackResource,
    StudentCatalog,
    StudentChapter,
    StudentCourse,
    StudentCourseDetail,
    StudentLecture,
    StudentPlaylist,
)
from app.services.course_storage import CourseStorage
from app.services.course_validity import access_expiry, validity_label
from app.services.courses import CourseContext, display_status_clause, effective_price
from app.services.vouchers import _field_error, _like

FUTURE_TOLERANCE = timedelta(minutes=5)


class AccessDenied(AppError):
    status_code = 403
    code = "course_access_denied"


def availability(course: Course, now: datetime) -> Availability:
    if course.status is CourseStatus.DRAFT:
        return Availability(purchasable=False, reason="draft", launch_at=course.launch_at)
    if course.status is CourseStatus.ARCHIVED:
        return Availability(purchasable=False, reason="archived", launch_at=course.launch_at)
    if course.launch_at is None or course.launch_at > now:
        return Availability(purchasable=False, reason="not_launched", launch_at=course.launch_at)
    return Availability(purchasable=True, reason=None, launch_at=course.launch_at)


class CourseEnrollmentService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        storage: CourseStorage | None,
        timezone: str,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.settings = settings
        self.storage = storage
        self.timezone = timezone
        self.clock = clock
        self.audit_repo = AuditLogRepository(session)

    # --- helpers -------------------------------------------------------------------------
    def _event(
        self,
        e: CourseEnrollment,
        event: str,
        actor: CourseActor,
        now: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            EnrollmentEvent(
                enrollment_id=e.id,
                event=event,
                actor=actor,
                details=details or None,
                occurred_at=now,
            )
        )

    def _audit(
        self, ctx: CourseContext, action: str, entity_id: int | None, changes: dict[str, Any]
    ) -> None:
        payload = dict(changes)
        if ctx.actor is CourseActor.COURSE_API:
            payload["via"] = "course_api"
        self.audit_repo.record(
            actor=ctx.audit_actor,
            action=action,
            entity_type="course_enrollment",
            entity_id=entity_id,
            request_id=ctx.request.request_id,
            client_ip=ctx.request.client_ip,
            changes=payload,
        )

    def _local(self, moment: datetime | None) -> str:
        return moment.astimezone(ZoneInfo(self.timezone)).strftime("%d %b %Y") if moment else ""

    @staticmethod
    def effective_status(e: CourseEnrollment, now: datetime) -> EnrollmentStatus:
        if (
            e.status is EnrollmentStatus.ACTIVE
            and e.access_expires_at is not None
            and e.access_expires_at <= now
        ):
            return EnrollmentStatus.EXPIRED
        return e.status

    def _read(self, e: CourseEnrollment, now: datetime) -> EnrollmentRead:
        return EnrollmentRead(
            id=e.id,
            course_id=e.course_id,
            course_title=e.course_title,
            student_id=e.student_id,
            student_name=e.student_name,
            source=e.source,
            status=self.effective_status(e, now),
            payment_provider=e.payment_provider,
            payment_id=e.payment_id,
            order_id=e.order_id,
            currency=e.currency,
            list_price=e.list_price,
            amount_paid=e.amount_paid,
            voucher_code=e.voucher_code,
            purchased_at=e.purchased_at,
            access_start_at=e.access_start_at,
            validity_value=e.validity_value,
            validity_unit=e.validity_unit,
            validity_label=validity_label(e.validity_value, e.validity_unit),
            access_expires_at=e.access_expires_at,
            status_reason=e.status_reason,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )

    def _course(self, course_id: int) -> Course:
        course = self.session.get(Course, course_id)
        if course is None:
            raise NotFoundError(f"Course {course_id} not found", code="course_not_found")
        return course

    def expire_due(self) -> int:
        """Mark active enrollments whose access has ended as expired (with a history entry).
        Purchases and learning history are kept; only the status changes."""
        now = self.clock()
        due = list(
            self.session.scalars(
                select(CourseEnrollment).where(
                    CourseEnrollment.status == EnrollmentStatus.ACTIVE,
                    CourseEnrollment.access_expires_at <= now,
                )
            )
        )
        if not due:
            self.session.rollback()
            return 0
        with transaction(self.session):
            for e in due:
                result = self.session.execute(
                    update(CourseEnrollment)
                    .where(
                        CourseEnrollment.id == e.id,
                        CourseEnrollment.status == EnrollmentStatus.ACTIVE,
                    )
                    .values(
                        status=EnrollmentStatus.EXPIRED,
                        updated_at=now,
                        version=CourseEnrollment.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    self._event(
                        e,
                        "expired",
                        CourseActor.SYSTEM,
                        now,
                        {
                            "access_expires_at": e.access_expires_at.isoformat()
                            if e.access_expires_at
                            else None
                        },
                    )
        self.session.expire_all()
        return len(due)

    # --- admin views -----------------------------------------------------------------------
    def list(
        self,
        *,
        course_id: int | None = None,
        status: EnrollmentStatus | None = None,
        q: str | None = None,
        purchased_from: datetime | None = None,
        purchased_to: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> EnrollmentPage:
        self.expire_due()
        now = self.clock()
        E = CourseEnrollment
        query = select(E)
        if course_id is not None:
            query = query.where(E.course_id == course_id)
        if status is not None:
            query = query.where(E.status == status)
        pattern = _like(q)
        if pattern:
            query = query.where(
                or_(
                    func.lower(E.student_id).like(pattern, escape="\\"),
                    func.lower(func.coalesce(E.student_name, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(E.payment_id, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(E.order_id, "")).like(pattern, escape="\\"),
                    func.lower(E.course_title).like(pattern, escape="\\"),
                )
            )
        if purchased_from is not None:
            query = query.where(E.purchased_at >= purchased_from)
        if purchased_to is not None:
            query = query.where(E.purchased_at < purchased_to)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(E.created_at.desc(), E.id.desc()).limit(limit).offset(offset)
        )
        return EnrollmentPage(
            items=[self._read(e, now) for e in rows], total=total, limit=limit, offset=offset
        )

    def detail(self, enrollment_id: int) -> EnrollmentDetail:
        self.expire_due()
        e = self.session.get(CourseEnrollment, enrollment_id)
        if e is None:
            raise NotFoundError(
                f"Enrollment {enrollment_id} not found", code="enrollment_not_found"
            )
        return EnrollmentDetail(
            **self._read(e, self.clock()).model_dump(),
            events=[EnrollmentEventRead.model_validate(ev) for ev in e.events],
        )

    def cancel(self, enrollment_id: int, reason: str, ctx: CourseContext) -> EnrollmentDetail:
        """An authorised admin action that ends a student's access (history is kept)."""
        with transaction(self.session):
            e = self.session.get(CourseEnrollment, enrollment_id)
            if e is None:
                raise NotFoundError(
                    f"Enrollment {enrollment_id} not found", code="enrollment_not_found"
                )
            now = self.clock()
            if self.effective_status(e, now) not in (
                EnrollmentStatus.ACTIVE,
                EnrollmentStatus.PENDING,
            ):
                raise ConflictError(
                    "Only active or pending enrollments can be cancelled", code="invalid_status"
                )
            e.status, e.status_reason = EnrollmentStatus.CANCELLED, reason
            self.session.flush()
            self._event(e, "cancelled", ctx.actor, now, {"reason": reason})
            self._audit(
                ctx, "course.enrollment.cancel", e.id, {"course_id": e.course_id, "reason": reason}
            )
        return self.detail(enrollment_id)

    def summary(self) -> CourseSummary:
        self.expire_due()
        now = self.clock()

        def count(*where: Any) -> int:
            return int(
                self.session.scalar(select(func.count()).select_from(Course).where(*where)) or 0
            )

        by_status = dict(
            self.session.execute(
                select(CourseEnrollment.status, func.count()).group_by(CourseEnrollment.status)
            ).all()
        )
        revenue = self.session.execute(
            select(CourseEnrollment.currency, func.sum(CourseEnrollment.amount_paid))
            .where(
                CourseEnrollment.status.in_((EnrollmentStatus.ACTIVE, EnrollmentStatus.EXPIRED)),
                CourseEnrollment.source == EnrollmentSource.PAYMENT,
            )
            .group_by(CourseEnrollment.currency)
            .order_by(CourseEnrollment.currency)
        ).all()
        return CourseSummary(
            total_courses=count(),
            published=count(display_status_clause("published", now)),
            scheduled=count(display_status_clause("scheduled", now)),
            draft=count(display_status_clause("draft", now)),
            archived=count(display_status_clause("archived", now)),
            total_enrollments=sum(by_status.values()),
            active_enrollments=by_status.get(EnrollmentStatus.ACTIVE, 0),
            expired_enrollments=by_status.get(EnrollmentStatus.EXPIRED, 0),
            pending_enrollments=by_status.get(EnrollmentStatus.PENDING, 0),
            revenue=[MoneyTotal(currency=c, amount=a or Decimal(0)) for c, a in revenue],
        )

    # --- purchases (trusted app backend) ---------------------------------------------------
    def _new_enrollment(
        self,
        course: Course,
        student_id: str,
        student_name: str | None,
        source: EnrollmentSource,
        status: EnrollmentStatus,
    ) -> CourseEnrollment:
        return CourseEnrollment(
            course_id=course.id,
            course_title=course.title,
            student_id=student_id,
            student_name=student_name,
            source=source,
            status=status,
            currency=course.currency,
            list_price=effective_price(course),
            amount_paid=Decimal(0),
            validity_value=course.validity_value,
            validity_unit=course.validity_unit,
        )

    def _activate(self, e: CourseEnrollment, purchased_at: datetime) -> None:
        """Access starts at this student's purchase, for the validity snapshot they bought."""
        e.status = EnrollmentStatus.ACTIVE
        e.purchased_at = e.access_start_at = purchased_at
        e.access_expires_at = access_expiry(
            purchased_at, e.validity_value, e.validity_unit, self.timezone
        )

    def _require_purchasable(self, course: Course, at: datetime) -> None:
        state = availability(course, at)
        if not state.purchasable:
            messages = {
                "draft": "This course is not available",
                "archived": "This course is archived and accepts no new enrollments",
                "not_launched": "This course has not launched yet",
            }
            raise ConflictError(
                messages[state.reason or "draft"],
                code=f"course_{state.reason}",
                details={"launch_at": course.launch_at.isoformat() if course.launch_at else None},
            )

    def create_order(
        self, body: OrderCreate, ctx: CourseContext, *, retry: bool = True
    ) -> EnrollmentResult:
        existing = self.session.scalar(
            select(CourseEnrollment).where(CourseEnrollment.order_id == body.order_id)
        )
        now = self.clock()
        if existing is not None:
            if existing.course_id != body.course_id or existing.student_id != body.student_id:
                raise ConflictError(
                    "This order id belongs to another purchase", code="order_conflict"
                )
            return EnrollmentResult(enrollment=self._read(existing, now), duplicate=True)
        course = self._course(body.course_id)
        if course.is_free:
            raise ConflictError(
                "This course is free; enroll without an order", code="course_is_free"
            )
        self._require_purchasable(course, now)
        e = self._new_enrollment(
            course,
            body.student_id,
            body.student_name,
            EnrollmentSource.PAYMENT,
            EnrollmentStatus.PENDING,
        )
        e.order_id, e.payment_provider = body.order_id, body.payment_provider
        try:
            with transaction(self.session):
                self.session.add(e)
                self.session.flush()
                self._event(
                    e,
                    "order_created",
                    ctx.actor,
                    now,
                    {
                        "order_id": body.order_id,
                        "list_price": str(e.list_price),
                        "validity": validity_label(e.validity_value, e.validity_unit),
                    },
                )
                self._audit(
                    ctx,
                    "course.enrollment.order",
                    e.id,
                    {"course_id": course.id, "order_id": body.order_id},
                )
        except IntegrityError:  # a concurrent call created it first: return that one
            self.session.rollback()
            if not retry:
                raise
            return self.create_order(body, ctx, retry=False)
        return EnrollmentResult(enrollment=self._read(e, now), duplicate=False)

    def confirm_payment(
        self, body: PaymentConfirm, ctx: CourseContext, *, retry: bool = True
    ) -> EnrollmentResult:
        """Called after the app backend verified the payment with the provider. One payment,
        one enrollment: a repeated callback returns the existing enrollment."""
        now = self.clock()
        duplicate = self.session.scalar(
            select(CourseEnrollment).where(CourseEnrollment.payment_id == body.payment_id)
        )
        if duplicate is not None:
            if duplicate.course_id != body.course_id or duplicate.student_id != body.student_id:
                raise ConflictError(
                    "This payment id belongs to another purchase", code="payment_conflict"
                )
            with transaction(self.session):
                self._event(
                    duplicate, "duplicate_callback", ctx.actor, now, {"payment_id": body.payment_id}
                )
            return EnrollmentResult(enrollment=self._read(duplicate, now), duplicate=True)

        paid_at = body.paid_at or now
        if paid_at > now + FUTURE_TOLERANCE:
            raise _field_error("paid_at", "The payment time is in the future", "invalid_paid_at")
        course = self._course(body.course_id)
        if course.is_free:
            raise ConflictError("This course is free; use free enrollment", code="course_is_free")
        pending = None
        if body.order_id:
            pending = self.session.scalar(
                select(CourseEnrollment).where(CourseEnrollment.order_id == body.order_id)
            )
            if pending is not None and (
                pending.course_id != course.id or pending.student_id != body.student_id
            ):
                raise ConflictError(
                    "This order id belongs to another purchase", code="order_conflict"
                )
            if pending is not None and pending.status is not EnrollmentStatus.PENDING:
                raise ConflictError(
                    f"This order is already {pending.status.value}", code="order_not_pending"
                )
        if pending is None:
            # No checkout record: the course must be purchasable when the student paid.
            self._require_purchasable(course, paid_at)
        e = pending or self._new_enrollment(
            course,
            body.student_id,
            body.student_name,
            EnrollmentSource.PAYMENT,
            EnrollmentStatus.PENDING,
        )
        if body.currency != e.currency:
            raise _field_error(
                "currency", f"This course is priced in {e.currency}", "currency_mismatch"
            )
        if body.amount_paid > e.list_price:
            raise _field_error(
                "amount_paid", f"More than the course price ({e.list_price})", "amount_mismatch"
            )
        if body.amount_paid < e.list_price and not body.voucher_code:
            raise _field_error(
                "amount_paid",
                "Less than the course price without a voucher code",
                "amount_mismatch",
            )
        e.payment_id, e.payment_provider = body.payment_id, body.payment_provider
        e.order_id = e.order_id or body.order_id
        e.amount_paid, e.voucher_code = body.amount_paid, body.voucher_code
        if body.student_name:
            e.student_name = body.student_name
        self._activate(e, paid_at)
        try:
            with transaction(self.session):
                if pending is None:
                    self.session.add(e)
                self.session.flush()
                self._event(
                    e,
                    "payment_confirmed",
                    ctx.actor,
                    now,
                    {
                        "payment_id": body.payment_id,
                        "provider": body.payment_provider,
                        "amount_paid": str(body.amount_paid),
                        "voucher_code": body.voucher_code,
                        "validity": validity_label(e.validity_value, e.validity_unit),
                        "access_expires_at": e.access_expires_at.isoformat()
                        if e.access_expires_at
                        else None,
                    },
                )
                self._audit(
                    ctx,
                    "course.enrollment.payment_confirmed",
                    e.id,
                    {
                        "course_id": course.id,
                        "payment_id": body.payment_id,
                        "amount_paid": str(body.amount_paid),
                    },
                )
        except IntegrityError:  # the same payment arrived concurrently: report that one
            self.session.rollback()
            if not retry:
                raise
            return self.confirm_payment(body, ctx, retry=False)
        return EnrollmentResult(enrollment=self._read(e, now), duplicate=False)

    def enroll_free(self, body: FreeEnroll, ctx: CourseContext) -> EnrollmentResult:
        now = self.clock()
        course = self._course(body.course_id)
        if not course.is_free:
            raise ConflictError(
                "This course is paid; complete a purchase first", code="course_is_paid"
            )
        self._require_purchasable(course, now)
        current = self._best(course.id, body.student_id, now)
        if current is not None and self.effective_status(current, now) is EnrollmentStatus.ACTIVE:
            return EnrollmentResult(enrollment=self._read(current, now), duplicate=True)
        e = self._new_enrollment(
            course,
            body.student_id,
            body.student_name,
            EnrollmentSource.FREE,
            EnrollmentStatus.PENDING,
        )
        self._activate(e, now)
        with transaction(self.session):
            self.session.add(e)
            self.session.flush()
            self._event(
                e,
                "enrolled_free",
                ctx.actor,
                now,
                {"validity": validity_label(e.validity_value, e.validity_unit)},
            )
            self._audit(ctx, "course.enrollment.free", e.id, {"course_id": course.id})
        return EnrollmentResult(enrollment=self._read(e, now), duplicate=False)

    def reverse_payment(self, body: PaymentReversal, ctx: CourseContext) -> EnrollmentRead:
        """Refund or cancellation reported by the app backend: access ends, history stays."""
        now = self.clock()
        with transaction(self.session):
            e = self.session.scalar(
                select(CourseEnrollment).where(CourseEnrollment.payment_id == body.payment_id)
            )
            if e is None:
                raise NotFoundError("No enrollment for this payment", code="enrollment_not_found")
            target = EnrollmentStatus(body.status)
            if e.status is target:
                return self._read(e, now)
            if e.status in (EnrollmentStatus.REFUNDED, EnrollmentStatus.CANCELLED):
                raise ConflictError(
                    f"This enrollment is already {e.status.value}", code="invalid_status"
                )
            e.status, e.status_reason = target, body.reason
            self.session.flush()
            self._event(
                e,
                body.status,
                ctx.actor,
                now,
                {"payment_id": body.payment_id, "reason": body.reason},
            )
            self._audit(
                ctx, f"course.enrollment.{body.status}", e.id, {"payment_id": body.payment_id}
            )
        return self._read(e, now)

    # --- student access ----------------------------------------------------------------------
    def _best(self, course_id: int, student_id: str, now: datetime) -> CourseEnrollment | None:
        """The enrollment that decides access: the active one ending last, else pending, else
        the latest of the rest."""
        rows = list(
            self.session.scalars(
                select(CourseEnrollment)
                .where(
                    CourseEnrollment.course_id == course_id,
                    CourseEnrollment.student_id == student_id,
                )
                .order_by(CourseEnrollment.id.desc())
            )
        )
        active = [e for e in rows if self.effective_status(e, now) is EnrollmentStatus.ACTIVE]
        if active:
            return max(active, key=lambda e: e.access_expires_at or now)
        pending = [e for e in rows if e.status is EnrollmentStatus.PENDING]
        return pending[0] if pending else (rows[0] if rows else None)

    def access(self, course_id: int, student_id: str) -> AccessState:
        now = self.clock()
        e = self._best(course_id, student_id, now)
        if e is None:
            return AccessState(
                status="none",
                access_expires_at=None,
                enrollment_id=None,
                message="Not enrolled in this course.",
            )
        status = self.effective_status(e, now)
        until = self._local(e.access_expires_at)
        messages = {
            EnrollmentStatus.ACTIVE: f"You have access until {until}.",
            EnrollmentStatus.PENDING: "Your payment is being confirmed. Access starts once it is.",
            EnrollmentStatus.EXPIRED: f"Your access to this course expired on {until}.",
            EnrollmentStatus.CANCELLED: "This enrollment was cancelled.",
            EnrollmentStatus.REFUNDED: "This purchase was refunded, so access has ended.",
        }
        return AccessState(
            status=status.value,
            access_expires_at=e.access_expires_at,  # type: ignore[arg-type]
            enrollment_id=e.id,
            message=messages[status],
        )

    def _visible(self, course_id: int) -> Course:
        course = self._course(course_id)
        if course.status is CourseStatus.DRAFT:
            raise NotFoundError(f"Course {course_id} not found", code="course_not_found")
        return course

    def _student_course(self, c: Course, student_id: str | None, now: datetime) -> StudentCourse:
        names = dict(
            self.session.execute(
                select(CourseTaxonomyTerm.id, CourseTaxonomyTerm.name).where(
                    CourseTaxonomyTerm.id.in_(
                        [i for i in (c.class_id, c.stream_id, c.subject_id) if i]
                    )
                )
            ).all()
        )
        thumb = None
        if c.thumbnail_asset_id and self.storage is not None:
            asset = self.session.get(CourseAsset, c.thumbnail_asset_id)
            if asset is not None and asset.status is CourseAssetStatus.UPLOADED:
                thumb = self.storage.signed_url(asset.storage_key)
        return StudentCourse(
            id=c.id,
            title=c.title,
            description=c.description,
            goals=c.goals,
            thumbnail_url=thumb,
            class_name=names.get(c.class_id),
            stream_name=names.get(c.stream_id),  # type: ignore[arg-type]
            subject_name=names.get(c.subject_id),  # type: ignore[arg-type]
            is_free=c.is_free,
            currency=c.currency,
            price=c.price,
            original_price=c.original_price,
            effective_price=effective_price(c),
            validity_value=c.validity_value,
            validity_unit=c.validity_unit,
            validity_label=validity_label(c.validity_value, c.validity_unit),
            availability=availability(c, now),
            access=self.access(c.id, student_id) if student_id else None,
        )

    def catalog(self, student_id: str | None = None) -> StudentCatalog:
        """Published courses (including ones launching later, marked not yet purchasable)."""
        now = self.clock()
        courses = self.session.scalars(
            select(Course)
            .where(Course.status == CourseStatus.PUBLISHED)
            .order_by(Course.launch_at.asc(), Course.id.asc())
        )
        return StudentCatalog(items=[self._student_course(c, student_id, now) for c in courses])

    def course_for_student(
        self, course_id: int, student_id: str | None = None, *, include_unpublished: bool = False
    ) -> StudentCourseDetail:
        now = self.clock()
        c = self._course(course_id) if include_unpublished else self._visible(course_id)
        base = self._student_course(c, student_id, now)
        playlists = []
        for p in c.playlists:
            chapters = []
            for ch in p.chapters:
                lectures = [
                    StudentLecture(
                        id=lec.id,
                        title=lec.title,
                        duration_seconds=lec.duration_seconds,
                        is_free_preview=lec.is_free_preview,
                        languages=[v.language for v in lec.variants],
                        resource_count=len(lec.resources),
                    )
                    for lec in ch.lectures
                    if include_unpublished or lec.status is LectureStatus.PUBLISHED
                ]
                if lectures or include_unpublished:
                    chapters.append(StudentChapter(id=ch.id, title=ch.title, lectures=lectures))
            if chapters or include_unpublished:
                playlists.append(
                    StudentPlaylist(
                        id=p.id, title=p.title, description=p.description, chapters=chapters
                    )
                )
        return StudentCourseDetail(**base.model_dump(), playlists=playlists)

    def student_enrollments(self, student_id: str) -> list[EnrollmentRead]:
        self.expire_due()
        now = self.clock()
        rows = self.session.scalars(
            select(CourseEnrollment)
            .where(CourseEnrollment.student_id == student_id)
            .order_by(CourseEnrollment.created_at.desc())
        )
        return [self._read(e, now) for e in rows]

    def _lecture_in(self, course: Course, lecture_id: int) -> Lecture:
        lec = self.session.get(Lecture, lecture_id)
        if lec is None or lec.chapter.playlist.course_id != course.id:
            raise NotFoundError("Lecture not found", code="lecture_not_found")
        return lec

    def _require_access(
        self, course: Course, lec: Lecture, student_id: str | None, *, preview: bool
    ) -> AccessState | None:
        """Free previews of a published course are open; everything else needs active
        access. Enforced here, on the server, for every video and file."""
        if preview:
            return None
        if lec.is_free_preview and course.status is CourseStatus.PUBLISHED:
            return self.access(course.id, student_id) if student_id else None
        if not student_id:
            raise AccessDenied("Sign in and enroll to watch this lecture", code="not_enrolled")
        state = self.access(course.id, student_id)
        if state.status != "active":
            codes = {
                "pending": "enrollment_pending",
                "expired": "course_access_expired",
                "cancelled": "enrollment_cancelled",
                "refunded": "enrollment_refunded",
                "none": "not_enrolled",
            }
            if state.status == "expired":
                self.expire_due()
            raise AccessDenied(
                state.message,
                code=codes[state.status],
                details={
                    "access_expires_at": state.access_expires_at.isoformat()
                    if state.access_expires_at
                    else None
                },
            )
        return state

    def _signed(self, asset_id: int | None, *, download_name: str | None = None) -> str | None:
        if asset_id is None:
            return None
        asset = self.session.get(CourseAsset, asset_id)
        if asset is None or asset.status is not CourseAssetStatus.UPLOADED:
            return None
        if self.storage is None:
            raise ServiceUnavailableError(
                "File storage is not configured", code="storage_not_configured"
            )
        return self.storage.signed_url(asset.storage_key, download_name=download_name)

    def playback(
        self,
        course_id: int,
        lecture_id: int,
        student_id: str | None,
        language: str | None,
        *,
        preview: bool = False,
    ) -> Playback:
        course = self._course(course_id) if preview else self._visible(course_id)
        lec = self._lecture_in(course, lecture_id)
        if lec.status is not LectureStatus.PUBLISHED and not preview:
            raise NotFoundError("Lecture not found", code="lecture_not_found")
        state = self._require_access(course, lec, student_id, preview=preview)
        available = [
            {"code": v.language, "name": LANGUAGES.get(v.language, v.language)}
            for v in lec.variants
        ]
        if not lec.variants:
            raise NotFoundError("This lecture has no video yet", code="lecture_without_video")
        wanted = language or lec.default_language or lec.variants[0].language
        variant = next((v for v in lec.variants if v.language == wanted), None)
        if variant is None:
            raise NotFoundError(
                f"This lecture is not available in {LANGUAGES.get(wanted, wanted)}. Available: "
                + ", ".join(a["name"] for a in available),
                code="language_unavailable",
                details={"available_languages": available},
            )
        video = variant.video_url or self._signed(variant.video_asset_id)
        if not video:
            raise NotFoundError("This video is not uploaded yet", code="lecture_without_video")
        return Playback(
            lecture_id=lec.id,
            title=lec.title,
            language=variant.language,
            language_name=LANGUAGES.get(variant.language, variant.language),
            available_languages=available,
            video_url=video,
            audio_url=self._signed(variant.audio_asset_id),
            subtitle_url=self._signed(variant.subtitle_asset_id),
            url_expires_in=None
            if variant.video_url
            else self.settings.course_assets_url_ttl_seconds,
            free_preview=lec.is_free_preview,
            access_expires_at=state.access_expires_at if state else None,
            resources=[
                PlaybackResource(id=r.id, title=r.title, is_downloadable=r.is_downloadable)
                for r in lec.resources
            ],
        )

    def download(
        self, course_id: int, resource_id: int, student_id: str | None, *, preview: bool = False
    ) -> DownloadLink:
        course = self._course(course_id) if preview else self._visible(course_id)
        r = self.session.get(LectureResource, resource_id)
        if r is None or r.lecture.chapter.playlist.course_id != course.id:
            raise NotFoundError("Resource not found", code="resource_not_found")
        lec = r.lecture
        if lec.status is not LectureStatus.PUBLISHED and not preview:
            raise NotFoundError("Resource not found", code="resource_not_found")
        self._require_access(course, lec, student_id, preview=preview)
        if not r.is_downloadable and not preview:
            raise PermissionDeniedError(
                "This material can be viewed but not downloaded", code="download_not_permitted"
            )
        asset = self.session.get(CourseAsset, r.asset_id)
        url = self._signed(r.asset_id, download_name=asset.original_filename if asset else None)
        if url is None:
            raise NotFoundError("This file is not uploaded yet", code="resource_not_uploaded")
        return DownloadLink(url=url, expires_in=self.settings.course_assets_url_ttl_seconds)


def enrollment_status_filter(value: str | None) -> EnrollmentStatus | None:
    if value is None:
        return None
    try:
        return EnrollmentStatus(value)
    except ValueError as exc:
        raise DomainValidationError("unknown status", code="invalid_status") from exc
