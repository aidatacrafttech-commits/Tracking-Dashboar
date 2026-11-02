"""Course management (admin side): taxonomy, courses, the content tree, publishing, files.

Rules enforced here (see docs/COURSES.md):
  * A course starts as a draft; publishing runs the publish check (mandatory information,
    at least one published lecture, every published lecture has a video in some language).
  * There is no course end date. `launch_at` only opens the course for purchase; each
    student's access is counted from their own purchase (see course_enrollments.py).
  * Editing a course (validity, price, content) never touches existing enrollments: they
    carry their own snapshot.
  * A course with enrollments cannot be deleted (archive it) or unpublished while students
    hold or await access.
"""

from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Literal, TypeVar

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.core.time import utcnow
from app.db.session import transaction
from app.models import (
    Chapter,
    Course,
    CourseAsset,
    CourseEnrollment,
    CourseTaxonomyTerm,
    Lecture,
    LectureResource,
    LectureVariant,
    Playlist,
)
from app.models.enums import (
    Actor,
    CourseActor,
    CourseAssetKind,
    CourseAssetStatus,
    CourseStatus,
    EnrollmentStatus,
    LectureStatus,
    TaxonomyKind,
)
from app.repositories.audit import AuditLogRepository
from app.schemas.course import (
    LANGUAGES,
    AssetRef,
    ChapterInput,
    ChapterRead,
    CourseCreate,
    CourseDetail,
    CoursePage,
    CourseRead,
    CourseUpdate,
    DisplayStatus,
    LectureInput,
    LectureRead,
    PlaylistInput,
    PlaylistRead,
    PublishCheck,
    PublishProblem,
    ResourceInput,
    ResourceRead,
    StorageStatus,
    TaxonomyCreate,
    TaxonomyRead,
    TaxonomyUpdate,
    UploadRequest,
    UploadTicket,
    VariantInput,
    VariantRead,
)
from app.services.context import RequestContext
from app.services.course_storage import ALLOWED_TYPES, MB, CourseStorage
from app.services.course_validity import validity_label
from app.services.vouchers import _field_error, _like

T = TypeVar("T")
SortKey = Literal["created", "updated", "title", "launch_at"]
OPEN_ENROLLMENT = (EnrollmentStatus.ACTIVE, EnrollmentStatus.PENDING)


@dataclass(frozen=True, slots=True)
class CourseContext:
    actor: CourseActor
    request: RequestContext

    @property
    def audit_actor(self) -> Actor:
        return Actor.OWNER if self.actor is CourseActor.OWNER else Actor.SYSTEM


def display_status(course: Course, now: datetime) -> DisplayStatus:
    if course.status is CourseStatus.PUBLISHED:
        return (
            "scheduled" if course.launch_at is not None and course.launch_at > now else "published"
        )
    return course.status.value  # type: ignore[return-value]


def display_status_clause(status: DisplayStatus, now: datetime):  # type: ignore[no-untyped-def]
    c = Course
    published = c.status == CourseStatus.PUBLISHED
    return {
        "draft": c.status == CourseStatus.DRAFT,
        "archived": c.status == CourseStatus.ARCHIVED,
        "scheduled": and_(published, c.launch_at > now),
        "published": and_(published, or_(c.launch_at.is_(None), c.launch_at <= now)),
    }[status]


def effective_price(course: Course) -> Decimal:
    if course.is_free:
        return Decimal(0)
    return (
        course.discounted_price
        if course.discounted_price is not None
        else course.price or Decimal(0)
    )


class CourseService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        storage: CourseStorage | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session = session
        self.settings = settings
        self.storage = storage
        self.clock = clock
        self.audit_repo = AuditLogRepository(session)

    # --- helpers ---------------------------------------------------------------------------
    def _audit(
        self,
        ctx: CourseContext,
        action: str,
        entity_type: str,
        entity_id: int | str | None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(changes or {})
        if ctx.actor is CourseActor.COURSE_API:
            payload["via"] = "course_api"
        self.audit_repo.record(
            actor=ctx.audit_actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=ctx.request.request_id,
            client_ip=ctx.request.client_ip,
            changes=payload or None,
        )

    def _get(self, model: type[T], id_: int, what: str) -> T:
        row = self.session.get(model, id_)
        if row is None:
            raise NotFoundError(
                f"{what} {id_} not found", code=f"{what.lower().replace(' ', '_')}_not_found"
            )
        return row

    def asset_ref(self, asset_id: int | None, *, with_url: bool = True) -> AssetRef | None:
        if asset_id is None:
            return None
        asset = self.session.get(CourseAsset, asset_id)
        if asset is None:
            return None
        url = None
        if with_url and self.storage is not None and asset.status is CourseAssetStatus.UPLOADED:
            url = self.storage.signed_url(asset.storage_key)
        return AssetRef(
            id=asset.id,
            kind=asset.kind,
            status=asset.status,
            filename=asset.original_filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            url=url,
        )

    def _require_asset(self, asset_id: int | None, kind: CourseAssetKind, field: str) -> int | None:
        if asset_id is None:
            return None
        asset = self.session.get(CourseAsset, asset_id)
        if asset is None or asset.kind is not kind:
            raise _field_error(field, f"Choose an uploaded {kind.value} file", "invalid_asset")
        if asset.status is not CourseAssetStatus.UPLOADED:
            raise _field_error(field, "This file has not finished uploading", "asset_not_uploaded")
        return asset.id

    def _require_term(self, term_id: int | None, kind: TaxonomyKind, field: str) -> int | None:
        if term_id is None:
            return None
        term = self.session.get(CourseTaxonomyTerm, term_id)
        if term is None or term.kind is not kind:
            raise _field_error(field, f"Choose a {kind.value} from the list", "invalid_taxonomy")
        return term.id

    # --- taxonomy --------------------------------------------------------------------------
    def list_taxonomy(self, kind: TaxonomyKind | None = None) -> list[TaxonomyRead]:
        T_ = CourseTaxonomyTerm
        query = select(T_).order_by(T_.kind, T_.sort_order, T_.name_key)
        if kind is not None:
            query = query.where(T_.kind == kind)
        terms = list(self.session.scalars(query))
        counts: dict[int, int] = {}
        for column in (Course.class_id, Course.stream_id, Course.subject_id):
            for term_id, n in self.session.execute(
                select(column, func.count()).where(column.is_not(None)).group_by(column)
            ):
                counts[term_id] = counts.get(term_id, 0) + n
        return [
            TaxonomyRead(
                id=t.id,
                kind=t.kind,
                name=t.name,
                sort_order=t.sort_order,
                is_active=t.is_active,
                course_count=counts.get(t.id, 0),
            )
            for t in terms
        ]

    def create_term(self, body: TaxonomyCreate, ctx: CourseContext) -> TaxonomyRead:
        term = CourseTaxonomyTerm(
            kind=body.kind,
            name=body.name,
            name_key=body.name.casefold(),
            sort_order=body.sort_order,
            is_active=True,
        )
        try:
            with transaction(self.session):
                self.session.add(term)
                self.session.flush()
                self._audit(
                    ctx,
                    "course.taxonomy.create",
                    "course_taxonomy",
                    term.id,
                    {"kind": term.kind.value, "name": term.name},
                )
        except IntegrityError as exc:
            raise ConflictError(
                f"{body.kind.value.title()} {body.name!r} already exists", code="duplicate_taxonomy"
            ) from exc
        return TaxonomyRead(
            id=term.id,
            kind=term.kind,
            name=term.name,
            sort_order=term.sort_order,
            is_active=term.is_active,
        )

    def update_term(self, term_id: int, body: TaxonomyUpdate, ctx: CourseContext) -> TaxonomyRead:
        try:
            with transaction(self.session):
                term = self._get(CourseTaxonomyTerm, term_id, "Taxonomy term")
                if body.name is not None:
                    term.name, term.name_key = body.name, body.name.casefold()
                if body.sort_order is not None:
                    term.sort_order = body.sort_order
                if body.is_active is not None:
                    term.is_active = body.is_active
                self.session.flush()
                self._audit(
                    ctx,
                    "course.taxonomy.update",
                    "course_taxonomy",
                    term.id,
                    body.model_dump(exclude_none=True),
                )
        except IntegrityError as exc:
            raise ConflictError("That name is already used", code="duplicate_taxonomy") from exc
        return next(t for t in self.list_taxonomy(term.kind) if t.id == term.id)

    def delete_term(self, term_id: int, ctx: CourseContext) -> None:
        with transaction(self.session):
            term = self._get(CourseTaxonomyTerm, term_id, "Taxonomy term")
            used = self.session.scalar(
                select(func.count())
                .select_from(Course)
                .where(
                    or_(
                        Course.class_id == term_id,
                        Course.stream_id == term_id,
                        Course.subject_id == term_id,
                    )
                )
            )
            if used:
                raise ConflictError(
                    f"{used} course(s) use this {term.kind.value}; deactivate it instead",
                    code="taxonomy_in_use",
                )
            self._audit(
                ctx,
                "course.taxonomy.delete",
                "course_taxonomy",
                term_id,
                {"kind": term.kind.value, "name": term.name},
            )
            self.session.delete(term)

    # --- course reads ------------------------------------------------------------------------
    def _counts(self, ids: list[int]) -> dict[int, dict[str, int]]:
        out = {i: {"playlists": 0, "lectures": 0, "enrollments": 0, "active": 0} for i in ids}
        if not ids:
            return out
        for cid, n in self.session.execute(
            select(Playlist.course_id, func.count())
            .where(Playlist.course_id.in_(ids))
            .group_by(Playlist.course_id)
        ):
            out[cid]["playlists"] = n
        for cid, n in self.session.execute(
            select(Playlist.course_id, func.count(Lecture.id))
            .join(Chapter, Chapter.playlist_id == Playlist.id)
            .join(Lecture, Lecture.chapter_id == Chapter.id)
            .where(Playlist.course_id.in_(ids))
            .group_by(Playlist.course_id)
        ):
            out[cid]["lectures"] = n
        now = self.clock()
        for cid, status, expires, n in self.session.execute(
            select(
                CourseEnrollment.course_id,
                CourseEnrollment.status,
                CourseEnrollment.access_expires_at > now,
                func.count(),
            )
            .where(CourseEnrollment.course_id.in_(ids))
            .group_by(
                CourseEnrollment.course_id,
                CourseEnrollment.status,
                CourseEnrollment.access_expires_at > now,
            )
        ):
            out[cid]["enrollments"] += n
            if status is EnrollmentStatus.ACTIVE and expires:
                out[cid]["active"] += n
        return out

    def _names(self, *ids: int | None) -> dict[int, str]:
        wanted = [i for i in ids if i is not None]
        if not wanted:
            return {}
        return dict(
            self.session.execute(
                select(CourseTaxonomyTerm.id, CourseTaxonomyTerm.name).where(
                    CourseTaxonomyTerm.id.in_(wanted)
                )
            ).all()
        )

    def _read(
        self, c: Course, counts: dict[str, int], names: dict[int, str], now: datetime
    ) -> CourseRead:
        return CourseRead(
            id=c.id,
            title=c.title,
            description=c.description,
            goals=c.goals,
            thumbnail=self.asset_ref(c.thumbnail_asset_id),
            class_id=c.class_id,
            class_name=names.get(c.class_id) if c.class_id else None,
            stream_id=c.stream_id,
            stream_name=names.get(c.stream_id) if c.stream_id else None,
            subject_id=c.subject_id,
            subject_name=names.get(c.subject_id) if c.subject_id else None,
            is_free=c.is_free,
            currency=c.currency,
            price=c.price,
            original_price=c.original_price,
            discounted_price=c.discounted_price,
            effective_price=effective_price(c),
            status=c.status,
            display_status=display_status(c, now),
            launch_at=c.launch_at,
            validity_value=c.validity_value,
            validity_unit=c.validity_unit,
            validity_label=validity_label(c.validity_value, c.validity_unit),
            published_at=c.published_at,
            archived_at=c.archived_at,
            playlist_count=counts["playlists"],
            lecture_count=counts["lectures"],
            enrollment_count=counts["enrollments"],
            active_enrollment_count=counts["active"],
            created_at=c.created_at,
            created_by=c.created_by,
            updated_at=c.updated_at,
            updated_by=c.updated_by,
            version=c.version,
            can_delete=counts["enrollments"] == 0,
        )

    def list(
        self,
        *,
        q: str | None = None,
        class_id: int | None = None,
        stream_id: int | None = None,
        subject_id: int | None = None,
        status: DisplayStatus | None = None,
        pricing: Literal["free", "paid"] | None = None,
        launch_from: datetime | None = None,
        launch_to: datetime | None = None,
        sort: SortKey = "created",
        order: str = "desc",
        limit: int = 25,
        offset: int = 0,
    ) -> CoursePage:
        now = self.clock()
        C = Course
        query = select(C)
        pattern = _like(q)
        if pattern:
            query = query.where(func.lower(C.title).like(pattern, escape="\\"))
        for column, value in (
            (C.class_id, class_id),
            (C.stream_id, stream_id),
            (C.subject_id, subject_id),
        ):
            if value is not None:
                query = query.where(column == value)
        if status:
            query = query.where(display_status_clause(status, now))
        if pricing:
            query = query.where(C.is_free.is_(pricing == "free"))
        if launch_from is not None:
            query = query.where(C.launch_at >= launch_from)
        if launch_to is not None:
            query = query.where(C.launch_at < launch_to)
        column = {
            "created": C.created_at,
            "updated": C.updated_at,
            "title": C.title,
            "launch_at": C.launch_at,
        }[sort]
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(
            self.session.scalars(
                query.order_by(column.asc() if order == "asc" else column.desc(), C.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        counts = self._counts([c.id for c in rows])
        names = self._names(*[i for c in rows for i in (c.class_id, c.stream_id, c.subject_id)])
        return CoursePage(
            items=[self._read(c, counts[c.id], names, now) for c in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def detail(self, course_id: int) -> CourseDetail:
        c = self._get(Course, course_id, "Course")
        now = self.clock()
        base = self._read(
            c, self._counts([c.id])[c.id], self._names(c.class_id, c.stream_id, c.subject_id), now
        )
        return CourseDetail(**base.model_dump(), playlists=[self._playlist(p) for p in c.playlists])

    def _variant(self, v: LectureVariant) -> VariantRead:
        return VariantRead(
            id=v.id,
            language=v.language,
            language_name=LANGUAGES.get(v.language, v.language),
            video=self.asset_ref(v.video_asset_id),
            video_url=v.video_url,
            audio=self.asset_ref(v.audio_asset_id),
            subtitle=self.asset_ref(v.subtitle_asset_id),
            thumbnail=self.asset_ref(v.thumbnail_asset_id),
            description=v.description,
        )

    def _lecture(self, lec: Lecture) -> LectureRead:
        return LectureRead(
            id=lec.id,
            title=lec.title,
            description=lec.description,
            thumbnail=self.asset_ref(lec.thumbnail_asset_id),
            duration_seconds=lec.duration_seconds,
            is_free_preview=lec.is_free_preview,
            status=lec.status,
            default_language=lec.default_language,
            position=lec.position,
            variants=[self._variant(v) for v in lec.variants],
            resources=[
                ResourceRead(
                    id=r.id,
                    title=r.title,
                    asset=self.asset_ref(r.asset_id),  # type: ignore[arg-type]
                    is_downloadable=r.is_downloadable,
                    position=r.position,
                )
                for r in lec.resources
            ],
        )

    def _chapter(self, ch: Chapter) -> ChapterRead:
        return ChapterRead(
            id=ch.id,
            title=ch.title,
            description=ch.description,
            thumbnail=self.asset_ref(ch.thumbnail_asset_id),
            position=ch.position,
            lectures=[self._lecture(lec) for lec in ch.lectures],
        )

    def _playlist(self, p: Playlist) -> PlaylistRead:
        return PlaylistRead(
            id=p.id,
            title=p.title,
            description=p.description,
            thumbnail=self.asset_ref(p.thumbnail_asset_id),
            position=p.position,
            chapters=[self._chapter(ch) for ch in p.chapters],
        )

    # --- course writes -----------------------------------------------------------------------
    def _apply(self, c: Course, body: CourseCreate | CourseUpdate) -> None:
        c.title, c.description, c.goals = body.title, body.description, body.goals
        c.thumbnail_asset_id = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )
        c.class_id = self._require_term(body.class_id, TaxonomyKind.CLASS, "class_id")
        c.stream_id = self._require_term(body.stream_id, TaxonomyKind.STREAM, "stream_id")
        c.subject_id = self._require_term(body.subject_id, TaxonomyKind.SUBJECT, "subject_id")
        if body.currency not in self.settings.supported_currencies:
            raise _field_error(
                "currency",
                f"Use one of: {', '.join(self.settings.supported_currencies)}",
                "unsupported_currency",
            )
        c.is_free, c.currency = body.is_free, body.currency
        c.price, c.original_price, c.discounted_price = (
            body.price,
            body.original_price,
            body.discounted_price,
        )
        c.launch_at = body.launch_at
        c.validity_value, c.validity_unit = body.validity_value, body.validity_unit

    @staticmethod
    def _snapshot(c: Course) -> dict[str, Any]:
        def j(v: Any) -> Any:
            return (
                v.isoformat()
                if isinstance(v, datetime)
                else (str(v) if isinstance(v, Decimal) else (v.value if hasattr(v, "value") else v))
            )

        fields = (
            "title",
            "class_id",
            "stream_id",
            "subject_id",
            "is_free",
            "currency",
            "price",
            "original_price",
            "discounted_price",
            "launch_at",
            "validity_value",
            "validity_unit",
            "thumbnail_asset_id",
            "status",
        )
        return {f: j(getattr(c, f)) for f in fields}

    def create(self, body: CourseCreate, ctx: CourseContext) -> CourseDetail:
        with transaction(self.session):
            c = Course(status=CourseStatus.DRAFT, created_by=ctx.actor, updated_by=ctx.actor)
            self._apply(c, body)
            self.session.add(c)
            self.session.flush()
            self._audit(ctx, "course.create", "course", c.id, {"after": self._snapshot(c)})
        return self.detail(c.id)

    def update(self, course_id: int, body: CourseUpdate, ctx: CourseContext) -> CourseDetail:
        try:
            with transaction(self.session):
                c = self._get(Course, course_id, "Course")
                if body.version != c.version:
                    raise ConflictError(
                        "This course changed since you opened it; reload and retry",
                        code="concurrent_modification",
                    )
                before = self._snapshot(c)
                self._apply(c, body)
                c.updated_by = ctx.actor
                after = self._snapshot(c)
                changed = {
                    k: {"before": before[k], "after": after[k]}
                    for k in after
                    if before[k] != after[k]
                }
                if c.status is CourseStatus.PUBLISHED:
                    problems = [p for p in self._problems(c) if p.blocking]
                    if problems:
                        raise ConflictError(
                            problems[0].message,
                            code="publish_requirements",
                            details=[p.model_dump() for p in problems],
                        )
                self.session.flush()
                self._audit(ctx, "course.update", "course", c.id, {"changed": changed})
        except StaleDataError as exc:
            raise ConflictError(
                "This course changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        return self.detail(course_id)

    def _enrollment_count(
        self, course_id: int, statuses: tuple[EnrollmentStatus, ...] | None = None
    ) -> int:
        query = (
            select(func.count())
            .select_from(CourseEnrollment)
            .where(CourseEnrollment.course_id == course_id)
        )
        if statuses:
            query = query.where(CourseEnrollment.status.in_(statuses))
        return int(self.session.scalar(query) or 0)

    def delete(self, course_id: int, ctx: CourseContext) -> None:
        with transaction(self.session):
            c = self._get(Course, course_id, "Course")
            if self._enrollment_count(course_id):
                raise ConflictError(
                    "Students have enrolled in this course; archive it instead",
                    code="course_has_enrollments",
                )
            self._audit(ctx, "course.delete", "course", course_id, {"before": self._snapshot(c)})
            self.session.delete(c)

    def _problems(self, c: Course) -> list[PublishProblem]:
        out: list[PublishProblem] = []

        def add(code: str, message: str, blocking: bool = True) -> None:
            out.append(PublishProblem(code=code, message=message, blocking=blocking))

        if not c.description:
            add("missing_description", "Add a course description.")
        if c.class_id is None:
            add("missing_class", "Choose a class.")
        if c.subject_id is None:
            add("missing_subject", "Choose a subject.")
        if c.launch_at is None:
            add("missing_launch_date", "Set the launch date.")
        if not c.is_free and (c.price is None or c.price <= 0):
            add("missing_price", "Set a price, or make the course free.")
        if c.thumbnail_asset_id is None:
            add("missing_thumbnail", "Add a course thumbnail (recommended).", blocking=False)
        if not c.playlists:
            add("no_playlists", "Add at least one playlist with lectures.")
        published = 0
        for p in c.playlists:
            if not p.chapters:
                add("empty_playlist", f"Playlist “{p.title}” has no chapters.", blocking=False)
            for ch in p.chapters:
                if not ch.lectures:
                    add("empty_chapter", f"Chapter “{ch.title}” has no lectures.", blocking=False)
                for lec in ch.lectures:
                    if lec.status is not LectureStatus.PUBLISHED:
                        continue
                    published += 1
                    playable = [v for v in lec.variants if self._has_video(v)]
                    if not playable:
                        add(
                            "lecture_without_video",
                            f"Lecture “{lec.title}” has no uploaded video in any language.",
                        )
                    elif lec.default_language and lec.default_language not in {
                        v.language for v in playable
                    }:
                        add(
                            "default_language_missing",
                            f"Lecture “{lec.title}”: the default language has no video.",
                        )
        if c.playlists and published == 0:
            add("no_published_lectures", "Publish at least one lecture.")
        return out

    def _has_video(self, v: LectureVariant) -> bool:
        if v.video_url:
            return True
        if v.video_asset_id is None:
            return False
        asset = self.session.get(CourseAsset, v.video_asset_id)
        return asset is not None and asset.status is CourseAssetStatus.UPLOADED

    def publish_check(self, course_id: int) -> PublishCheck:
        problems = self._problems(self._get(Course, course_id, "Course"))
        return PublishCheck(ready=not any(p.blocking for p in problems), problems=problems)

    def _transition(self, course_id: int, action: str, ctx: CourseContext) -> CourseDetail:
        try:
            with transaction(self.session):
                c = self._get(Course, course_id, "Course")
                now = self.clock()
                if action == "publish":
                    if c.status is not CourseStatus.DRAFT:
                        raise ConflictError("Only a draft can be published", code="invalid_status")
                    blocking = [p for p in self._problems(c) if p.blocking]
                    if blocking:
                        raise ConflictError(
                            blocking[0].message,
                            code="publish_requirements",
                            details=[p.model_dump() for p in blocking],
                        )
                    c.status, c.published_at = CourseStatus.PUBLISHED, now
                elif action == "unpublish":
                    if c.status is not CourseStatus.PUBLISHED:
                        raise ConflictError(
                            "Only a published course can be unpublished", code="invalid_status"
                        )
                    if self._enrollment_count(course_id, OPEN_ENROLLMENT):
                        raise ConflictError(
                            "Students hold or await access; archive the course instead",
                            code="course_has_enrollments",
                        )
                    c.status = CourseStatus.DRAFT
                elif action == "archive":
                    if c.status is CourseStatus.ARCHIVED:
                        raise ConflictError("The course is already archived", code="invalid_status")
                    c.status, c.archived_at = CourseStatus.ARCHIVED, now
                else:  # unarchive
                    if c.status is not CourseStatus.ARCHIVED:
                        raise ConflictError(
                            "Only an archived course can be restored", code="invalid_status"
                        )
                    c.status = CourseStatus.DRAFT
                c.updated_by = ctx.actor
                self.session.flush()
                self._audit(ctx, f"course.{action}", "course", c.id, {"title": c.title})
        except StaleDataError as exc:
            raise ConflictError(
                "This course changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        return self.detail(course_id)

    def publish(self, course_id: int, ctx: CourseContext) -> CourseDetail:
        return self._transition(course_id, "publish", ctx)

    def unpublish(self, course_id: int, ctx: CourseContext) -> CourseDetail:
        return self._transition(course_id, "unpublish", ctx)

    def archive(self, course_id: int, ctx: CourseContext) -> CourseDetail:
        return self._transition(course_id, "archive", ctx)

    def unarchive(self, course_id: int, ctx: CourseContext) -> CourseDetail:
        return self._transition(course_id, "unarchive", ctx)

    # --- content ---------------------------------------------------------------------------------
    def _touch(self, course: Course, ctx: CourseContext) -> None:
        """Content edits bump the course version (so stale course forms are detected)."""
        course.updated_by = ctx.actor
        course.updated_at = self.clock()

    def _guard_published(self, course: Course) -> None:
        if course.status is CourseStatus.PUBLISHED:
            blocking = [p for p in self._problems(course) if p.blocking]
            if blocking:
                raise ConflictError(
                    f"A published course must stay complete: {blocking[0].message}",
                    code="publish_requirements",
                    details=[p.model_dump() for p in blocking],
                )

    def _content_write(
        self,
        ctx: CourseContext,
        action: str,
        course: Course,
        entity_id: int | None,
        details: dict[str, Any],
        fn: Callable[[], Any],
    ) -> Any:
        try:
            with transaction(self.session):
                result = fn()
                self.session.flush()
                self.session.expire(course)
                self._guard_published(course)
                self._touch(course, ctx)
                self._audit(
                    ctx,
                    f"course.content.{action}",
                    "course",
                    course.id,
                    {
                        **details,
                        "id": entity_id if entity_id is not None else getattr(result, "id", None),
                    },
                )
                self.session.flush()
        except StaleDataError as exc:
            raise ConflictError(
                "This course changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        return result

    @staticmethod
    def _next_position(items: list[Any]) -> int:
        return max((i.position for i in items), default=-1) + 1

    def add_playlist(self, course_id: int, body: PlaylistInput, ctx: CourseContext) -> PlaylistRead:
        course = self._get(Course, course_id, "Course")
        thumb = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )

        def fn() -> Playlist:
            p = Playlist(
                course_id=course.id,
                title=body.title,
                description=body.description,
                thumbnail_asset_id=thumb,
                position=self._next_position(course.playlists),
            )
            self.session.add(p)
            return p

        p = self._content_write(ctx, "playlist.create", course, None, {"title": body.title}, fn)
        return self._playlist(p)

    def update_playlist(
        self, playlist_id: int, body: PlaylistInput, ctx: CourseContext
    ) -> PlaylistRead:
        p = self._get(Playlist, playlist_id, "Playlist")
        thumb = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )

        def fn() -> Playlist:
            p.title, p.description, p.thumbnail_asset_id = body.title, body.description, thumb
            return p

        self._content_write(ctx, "playlist.update", p.course, p.id, {"title": body.title}, fn)
        return self._playlist(p)

    def delete_playlist(self, playlist_id: int, ctx: CourseContext) -> None:
        p = self._get(Playlist, playlist_id, "Playlist")
        course = p.course
        self._content_write(
            ctx, "playlist.delete", course, p.id, {"title": p.title}, lambda: self.session.delete(p)
        )

    def add_chapter(self, playlist_id: int, body: ChapterInput, ctx: CourseContext) -> ChapterRead:
        p = self._get(Playlist, playlist_id, "Playlist")
        thumb = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )

        def fn() -> Chapter:
            ch = Chapter(
                playlist_id=p.id,
                title=body.title,
                description=body.description,
                thumbnail_asset_id=thumb,
                position=self._next_position(p.chapters),
            )
            self.session.add(ch)
            return ch

        ch = self._content_write(ctx, "chapter.create", p.course, None, {"title": body.title}, fn)
        return self._chapter(ch)

    def update_chapter(
        self, chapter_id: int, body: ChapterInput, ctx: CourseContext
    ) -> ChapterRead:
        ch = self._get(Chapter, chapter_id, "Chapter")
        thumb = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )

        def fn() -> Chapter:
            ch.title, ch.description, ch.thumbnail_asset_id = body.title, body.description, thumb
            return ch

        self._content_write(
            ctx, "chapter.update", ch.playlist.course, ch.id, {"title": body.title}, fn
        )
        return self._chapter(ch)

    def delete_chapter(self, chapter_id: int, ctx: CourseContext) -> None:
        ch = self._get(Chapter, chapter_id, "Chapter")
        self._content_write(
            ctx,
            "chapter.delete",
            ch.playlist.course,
            ch.id,
            {"title": ch.title},
            lambda: self.session.delete(ch),
        )

    def _lecture_fields(self, lec: Lecture, body: LectureInput) -> None:
        lec.title, lec.description = body.title, body.description
        lec.thumbnail_asset_id = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )
        lec.duration_seconds, lec.is_free_preview = body.duration_seconds, body.is_free_preview
        lec.status, lec.default_language = body.status, body.default_language

    def add_lecture(self, chapter_id: int, body: LectureInput, ctx: CourseContext) -> LectureRead:
        ch = self._get(Chapter, chapter_id, "Chapter")

        def fn() -> Lecture:
            lec = Lecture(chapter_id=ch.id, position=self._next_position(ch.lectures))
            self._lecture_fields(lec, body)
            self.session.add(lec)
            return lec

        lec = self._content_write(
            ctx, "lecture.create", ch.playlist.course, None, {"title": body.title}, fn
        )
        return self._lecture(lec)

    def update_lecture(
        self, lecture_id: int, body: LectureInput, ctx: CourseContext
    ) -> LectureRead:
        lec = self._get(Lecture, lecture_id, "Lecture")

        def fn() -> Lecture:
            self._lecture_fields(lec, body)
            return lec

        self._content_write(
            ctx,
            "lecture.update",
            lec.chapter.playlist.course,
            lec.id,
            {"title": body.title, "status": body.status.value},
            fn,
        )
        return self._lecture(lec)

    def delete_lecture(self, lecture_id: int, ctx: CourseContext) -> None:
        lec = self._get(Lecture, lecture_id, "Lecture")
        self._content_write(
            ctx,
            "lecture.delete",
            lec.chapter.playlist.course,
            lec.id,
            {"title": lec.title},
            lambda: self.session.delete(lec),
        )

    def _variant_fields(self, v: LectureVariant, body: VariantInput) -> None:
        v.language = body.language
        v.video_asset_id = self._require_asset(
            body.video_asset_id, CourseAssetKind.VIDEO, "video_asset_id"
        )
        v.video_url = body.video_url
        v.audio_asset_id = self._require_asset(
            body.audio_asset_id, CourseAssetKind.AUDIO, "audio_asset_id"
        )
        v.subtitle_asset_id = self._require_asset(
            body.subtitle_asset_id, CourseAssetKind.SUBTITLE, "subtitle_asset_id"
        )
        v.thumbnail_asset_id = self._require_asset(
            body.thumbnail_asset_id, CourseAssetKind.IMAGE, "thumbnail_asset_id"
        )
        v.description = body.description
        if v.video_asset_id is None and v.video_url is None:
            raise _field_error(
                "video_asset_id", "Upload a video or enter a video URL", "missing_video"
            )

    def add_variant(self, lecture_id: int, body: VariantInput, ctx: CourseContext) -> VariantRead:
        lec = self._get(Lecture, lecture_id, "Lecture")
        if any(v.language == body.language for v in lec.variants):
            raise _field_error(
                "language",
                f"{LANGUAGES[body.language]} already exists for this lecture",
                "duplicate_language",
            )

        def fn() -> LectureVariant:
            v = LectureVariant(lecture_id=lec.id)
            self._variant_fields(v, body)
            self.session.add(v)
            if lec.default_language is None:
                lec.default_language = body.language
            return v

        v = self._content_write(
            ctx,
            "variant.create",
            lec.chapter.playlist.course,
            None,
            {"lecture_id": lec.id, "language": body.language},
            fn,
        )
        return self._variant(v)

    def update_variant(
        self, variant_id: int, body: VariantInput, ctx: CourseContext
    ) -> VariantRead:
        v = self._get(LectureVariant, variant_id, "Variant")
        lec = v.lecture
        if body.language != v.language and any(o.language == body.language for o in lec.variants):
            raise _field_error(
                "language",
                f"{LANGUAGES[body.language]} already exists for this lecture",
                "duplicate_language",
            )

        def fn() -> LectureVariant:
            old = v.language
            self._variant_fields(v, body)
            if lec.default_language == old:
                lec.default_language = v.language
            return v

        self._content_write(
            ctx,
            "variant.update",
            lec.chapter.playlist.course,
            v.id,
            {"lecture_id": lec.id, "language": body.language},
            fn,
        )
        return self._variant(v)

    def delete_variant(self, variant_id: int, ctx: CourseContext) -> None:
        v = self._get(LectureVariant, variant_id, "Variant")
        lec = v.lecture

        def fn() -> None:
            language = v.language
            lec.variants.remove(v)
            self.session.delete(v)
            if lec.default_language == language:
                lec.default_language = lec.variants[0].language if lec.variants else None

        self._content_write(
            ctx,
            "variant.delete",
            lec.chapter.playlist.course,
            v.id,
            {"lecture_id": lec.id, "language": v.language},
            fn,
        )

    def add_resource(
        self, lecture_id: int, body: ResourceInput, ctx: CourseContext
    ) -> ResourceRead:
        lec = self._get(Lecture, lecture_id, "Lecture")
        asset_id = self._require_asset(body.asset_id, CourseAssetKind.DOCUMENT, "asset_id")

        def fn() -> LectureResource:
            r = LectureResource(
                lecture_id=lec.id,
                title=body.title.strip(),
                asset_id=asset_id,
                is_downloadable=body.is_downloadable,
                position=self._next_position(lec.resources),
            )
            self.session.add(r)
            return r

        r = self._content_write(
            ctx,
            "resource.create",
            lec.chapter.playlist.course,
            None,
            {"lecture_id": lec.id, "title": body.title},
            fn,
        )
        return ResourceRead(
            id=r.id,
            title=r.title,
            asset=self.asset_ref(r.asset_id),  # type: ignore[arg-type]
            is_downloadable=r.is_downloadable,
            position=r.position,
        )

    def update_resource(
        self, resource_id: int, body: ResourceInput, ctx: CourseContext
    ) -> ResourceRead:
        r = self._get(LectureResource, resource_id, "Resource")
        asset_id = self._require_asset(body.asset_id, CourseAssetKind.DOCUMENT, "asset_id")

        def fn() -> LectureResource:
            r.title, r.asset_id, r.is_downloadable = (
                body.title.strip(),
                asset_id,
                body.is_downloadable,
            )  # type: ignore[assignment]
            return r

        self._content_write(
            ctx,
            "resource.update",
            r.lecture.chapter.playlist.course,
            r.id,
            {"title": body.title},
            fn,
        )
        return ResourceRead(
            id=r.id,
            title=r.title,
            asset=self.asset_ref(r.asset_id),  # type: ignore[arg-type]
            is_downloadable=r.is_downloadable,
            position=r.position,
        )

    def delete_resource(self, resource_id: int, ctx: CourseContext) -> None:
        r = self._get(LectureResource, resource_id, "Resource")
        self._content_write(
            ctx,
            "resource.delete",
            r.lecture.chapter.playlist.course,
            r.id,
            {"title": r.title},
            lambda: self.session.delete(r),
        )

    def _reorder(self, children: list[Any], ids: list[int], what: str) -> None:
        if sorted(ids) != sorted(c.id for c in children) or len(set(ids)) != len(ids):
            raise _field_error(
                "ids", f"Send every {what} of this parent exactly once", "invalid_order"
            )
        by_id = {c.id: c for c in children}
        for position, id_ in enumerate(ids):
            by_id[id_].position = position

    def reorder_playlists(self, course_id: int, ids: list[int], ctx: CourseContext) -> None:
        course = self._get(Course, course_id, "Course")
        self._content_write(
            ctx,
            "playlist.reorder",
            course,
            course.id,
            {"ids": ids},
            lambda: self._reorder(course.playlists, ids, "playlist"),
        )

    def reorder_chapters(self, playlist_id: int, ids: list[int], ctx: CourseContext) -> None:
        p = self._get(Playlist, playlist_id, "Playlist")
        self._content_write(
            ctx,
            "chapter.reorder",
            p.course,
            p.id,
            {"ids": ids},
            lambda: self._reorder(p.chapters, ids, "chapter"),
        )

    def reorder_lectures(self, chapter_id: int, ids: list[int], ctx: CourseContext) -> None:
        ch = self._get(Chapter, chapter_id, "Chapter")
        self._content_write(
            ctx,
            "lecture.reorder",
            ch.playlist.course,
            ch.id,
            {"ids": ids},
            lambda: self._reorder(ch.lectures, ids, "lecture"),
        )

    def reorder_resources(self, lecture_id: int, ids: list[int], ctx: CourseContext) -> None:
        lec = self._get(Lecture, lecture_id, "Lecture")
        self._content_write(
            ctx,
            "resource.reorder",
            lec.chapter.playlist.course,
            lec.id,
            {"ids": ids},
            lambda: self._reorder(lec.resources, ids, "resource"),
        )

    # --- files ---------------------------------------------------------------------------------
    def storage_status(self) -> StorageStatus:
        return StorageStatus(
            configured=self.storage is not None,
            max_video_mb=self.settings.course_asset_max_video_mb,
            max_file_mb=self.settings.course_asset_max_file_mb,
            allowed_types={k.value: sorted(v) for k, v in ALLOWED_TYPES.items()},
        )

    def request_upload(self, body: UploadRequest, ctx: CourseContext) -> UploadTicket:
        if self.storage is None:
            raise ServiceUnavailableError(
                "File storage is not configured (set COURSE_ASSETS_BUCKET on the server)",
                code="storage_not_configured",
            )
        allowed = ALLOWED_TYPES[body.kind]
        content_type = body.content_type.lower().split(";")[0].strip()
        ext = PurePosixPath(body.filename.replace("\\", "/")).suffix.lower()
        if content_type not in allowed:
            raise _field_error(
                "content_type",
                f"{body.kind.value.title()} files must be: "
                f"{', '.join(sorted({e for es in allowed.values() for e in es}))}",
                "invalid_file_type",
            )
        if ext not in allowed[content_type]:
            raise _field_error(
                "filename", f"The file extension does not match {content_type}", "invalid_file_type"
            )
        limit_mb = (
            self.settings.course_asset_max_video_mb
            if body.kind is CourseAssetKind.VIDEO
            else self.settings.course_asset_max_file_mb
        )
        if body.size_bytes > limit_mb * MB:
            raise _field_error(
                "size_bytes", f"The file is larger than {limit_mb} MB", "file_too_large"
            )
        now = self.clock()
        # The key never contains the user's filename (no path tricks, no PII in the bucket).
        folder = f"{self.settings.course_assets_prefix}{body.kind.value}/{now:%Y/%m}"
        key = f"{folder}/{uuid.uuid4().hex}{ext}"
        safe_name = (
            "".join(
                ch
                for ch in PurePosixPath(body.filename.replace("\\", "/")).name
                if ch.isprintable()
            )[:200]
            or f"file{ext}"
        )
        asset = CourseAsset(
            kind=body.kind,
            status=CourseAssetStatus.PENDING,
            storage_key=key,
            content_type=content_type,
            size_bytes=body.size_bytes,
            original_filename=safe_name,
            created_at=now,
        )
        with transaction(self.session):
            self.session.add(asset)
            self.session.flush()
            self._audit(
                ctx,
                "course.asset.upload_requested",
                "course_asset",
                asset.id,
                {"kind": body.kind.value, "size_bytes": body.size_bytes},
            )
        ticket = self.storage.presigned_upload(key, content_type, body.size_bytes)
        return UploadTicket(
            asset=self.asset_ref(asset.id, with_url=False),  # type: ignore[arg-type]
            upload_url=ticket["url"],
            fields=ticket["fields"],
            expires_in=self.settings.course_assets_url_ttl_seconds,
        )

    def complete_upload(self, asset_id: int, ctx: CourseContext) -> AssetRef:
        if self.storage is None:
            raise ServiceUnavailableError(
                "File storage is not configured", code="storage_not_configured"
            )
        asset = self._get(CourseAsset, asset_id, "Asset")
        if asset.status is CourseAssetStatus.UPLOADED:
            return self.asset_ref(asset.id)  # type: ignore[return-value]
        stored = self.storage.head(asset.storage_key)
        if stored is None:
            raise ConflictError(
                "The file has not reached storage yet; upload it first", code="upload_missing"
            )
        if (
            stored.size_bytes != asset.size_bytes
            or stored.content_type.split(";")[0] != asset.content_type
        ):
            raise ConflictError(
                "The stored file does not match the declared size or type", code="upload_mismatch"
            )
        with transaction(self.session):
            asset.status, asset.uploaded_at = CourseAssetStatus.UPLOADED, self.clock()
            self._audit(
                ctx,
                "course.asset.uploaded",
                "course_asset",
                asset.id,
                {"kind": asset.kind.value, "size_bytes": asset.size_bytes},
            )
        return self.asset_ref(asset.id)  # type: ignore[return-value]

    def asset_url(self, asset_id: int) -> AssetRef:
        ref = self.asset_ref(self._get(CourseAsset, asset_id, "Asset").id)
        assert ref is not None  # noqa: S101
        return ref


def guess_content_type(filename: str) -> str:
    """Best-effort type for clients that send none (the UI always sends the browser's)."""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"
