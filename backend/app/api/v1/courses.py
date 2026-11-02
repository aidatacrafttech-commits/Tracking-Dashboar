"""Course management endpoints.

/api/v1/courses/...               dashboard: owner writes (session + CSRF or owner bearer),
                                  owner and viewer read.
/api/v1/integrations/courses/...  the trusted app backend (COURSE_API_TOKEN bearer only): it
                                  authenticates students and verifies payments, then calls
                                  these to record orders/payments and to check access.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_settings, owner_context, require_read
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import request_id_var
from app.core.security import client_ip
from app.models.enums import Actor, CourseActor, EnrollmentStatus, TaxonomyKind
from app.schemas.common import UTCInput
from app.schemas.course import (
    AccessState,
    AssetRef,
    CancelRequest,
    ChapterInput,
    ChapterRead,
    CourseCreate,
    CourseDetail,
    CoursePage,
    CourseSummary,
    CourseUpdate,
    DisplayStatus,
    DownloadLink,
    EnrollmentDetail,
    EnrollmentPage,
    EnrollmentRead,
    EnrollmentResult,
    FreeEnroll,
    LectureInput,
    LectureRead,
    OrderCreate,
    OrderInput,
    PaymentConfirm,
    PaymentReversal,
    Playback,
    PlaylistInput,
    PlaylistRead,
    PublishCheck,
    ResourceInput,
    ResourceRead,
    StorageStatus,
    StudentCatalog,
    StudentCourseDetail,
    TaxonomyCreate,
    TaxonomyRead,
    TaxonomyUpdate,
    UploadRequest,
    UploadTicket,
    VariantInput,
    VariantRead,
)
from app.services.context import RequestContext
from app.services.course_enrollments import CourseEnrollmentService
from app.services.courses import CourseContext, CourseService, SortKey
from app.services.settings import SettingsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses", tags=["courses"])
integration_router = APIRouter(prefix="/integrations/courses", tags=["course integration"])

Id = Annotated[int, Path(ge=1)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Search = Annotated[str | None, Query(max_length=100)]
StudentId = Annotated[
    str, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
]
OptionalStudentId = Annotated[
    str | None, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
]
Language = Annotated[str | None, Query(max_length=16, pattern=r"^[a-z]{2,3}$")]
_bearer = HTTPBearer(auto_error=False)
NO_CONTENT = Response(status_code=status.HTTP_204_NO_CONTENT)


def _timezone(db: Session, settings: Settings) -> str:
    return SettingsService(db, settings).display_timezone()


def course_service(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> CourseService:
    return CourseService(db, settings, request.app.state.course_storage)


def enrollment_service(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> CourseEnrollmentService:
    return CourseEnrollmentService(
        db, settings, request.app.state.course_storage, _timezone(db, settings)
    )


def owner_course_context(ctx: RequestContext = Depends(owner_context)) -> CourseContext:
    return CourseContext(CourseActor.OWNER, ctx)


def course_api_context(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> CourseContext:
    """Only COURSE_API_TOKEN is accepted (never dashboard sessions or other tokens).
    Failed attempts are rate-limited like every other bearer failure."""
    settings: Settings = request.app.state.settings
    ip = client_ip(request)
    if settings.course_api_token is None:
        raise AuthenticationError(
            "The course API is not enabled (set COURSE_API_TOKEN on the server)",
            code="course_api_disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthenticationError("A course API bearer token is required")
    limiter = request.app.state.attempt_limiter
    limiter.check(ip, "course_api")
    expected = settings.course_api_token.get_secret_value()
    if not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        limiter.record(ip, "course_api", False)
        logger.warning("course API authentication failed", extra={"client_ip": ip})
        raise AuthenticationError("Invalid course API token")
    return CourseContext(
        CourseActor.COURSE_API,
        RequestContext(actor=Actor.SYSTEM, request_id=request_id_var.get(), client_ip=ip),
    )


Owner = Annotated[CourseContext, Depends(owner_course_context)]
Courses = Annotated[CourseService, Depends(course_service)]
Enrollments = Annotated[CourseEnrollmentService, Depends(enrollment_service)]
Read = [Depends(require_read)]

# --- summary, storage, files ----------------------------------------------------------------


@router.get("/summary", response_model=CourseSummary, dependencies=Read)
def summary(enrollments: Enrollments) -> CourseSummary:
    return enrollments.summary()


@router.get("/storage", response_model=StorageStatus, dependencies=Read)
def storage(courses: Courses) -> StorageStatus:
    return courses.storage_status()


@router.post("/assets/uploads", response_model=UploadTicket, status_code=201)
def request_upload(body: UploadRequest, ctx: Owner, courses: Courses) -> UploadTicket:
    return courses.request_upload(body, ctx)


@router.post("/assets/{asset_id}/complete", response_model=AssetRef)
def complete_upload(asset_id: Id, ctx: Owner, courses: Courses) -> AssetRef:
    return courses.complete_upload(asset_id, ctx)


@router.get("/assets/{asset_id}", response_model=AssetRef, dependencies=Read)
def asset(asset_id: Id, courses: Courses) -> AssetRef:
    return courses.asset_url(asset_id)


# --- taxonomy ---------------------------------------------------------------------------------


@router.get("/taxonomy", response_model=list[TaxonomyRead], dependencies=Read)
def list_taxonomy(courses: Courses, kind: TaxonomyKind | None = None) -> list[TaxonomyRead]:
    return courses.list_taxonomy(kind)


@router.post("/taxonomy", response_model=TaxonomyRead, status_code=201)
def create_term(body: TaxonomyCreate, ctx: Owner, courses: Courses) -> TaxonomyRead:
    return courses.create_term(body, ctx)


@router.put("/taxonomy/{term_id}", response_model=TaxonomyRead)
def update_term(term_id: Id, body: TaxonomyUpdate, ctx: Owner, courses: Courses) -> TaxonomyRead:
    return courses.update_term(term_id, body, ctx)


@router.delete("/taxonomy/{term_id}", status_code=204)
def delete_term(term_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_term(term_id, ctx)
    return NO_CONTENT


# --- enrollments (read-only, plus an explicit admin cancel) ------------------------------------


@router.get("/enrollments", response_model=EnrollmentPage, dependencies=Read)
def list_enrollments(
    enrollments: Enrollments,
    course_id: Annotated[int | None, Query(ge=1)] = None,
    status_filter: Annotated[EnrollmentStatus | None, Query(alias="status")] = None,
    q: Search = None,
    purchased_from: Annotated[UTCInput | None, Query()] = None,
    purchased_to: Annotated[UTCInput | None, Query()] = None,
    limit: Limit = 25,
    offset: Offset = 0,
) -> EnrollmentPage:
    return enrollments.list(
        course_id=course_id,
        status=status_filter,
        q=q,
        purchased_from=purchased_from,
        purchased_to=purchased_to,
        limit=limit,
        offset=offset,
    )


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentDetail, dependencies=Read)
def enrollment(enrollment_id: Id, enrollments: Enrollments) -> EnrollmentDetail:
    return enrollments.detail(enrollment_id)


@router.post("/enrollments/{enrollment_id}/cancel", response_model=EnrollmentDetail)
def cancel_enrollment(
    enrollment_id: Id, body: CancelRequest, ctx: Owner, enrollments: Enrollments
) -> EnrollmentDetail:
    return enrollments.cancel(enrollment_id, body.reason, ctx)


# --- content (playlists, chapters, lectures, language variants, resources) -------------------


@router.put("/playlists/{playlist_id}", response_model=PlaylistRead)
def update_playlist(
    playlist_id: Id, body: PlaylistInput, ctx: Owner, courses: Courses
) -> PlaylistRead:
    return courses.update_playlist(playlist_id, body, ctx)


@router.delete("/playlists/{playlist_id}", status_code=204)
def delete_playlist(playlist_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_playlist(playlist_id, ctx)
    return NO_CONTENT


@router.post("/playlists/{playlist_id}/chapters", response_model=ChapterRead, status_code=201)
def add_chapter(playlist_id: Id, body: ChapterInput, ctx: Owner, courses: Courses) -> ChapterRead:
    return courses.add_chapter(playlist_id, body, ctx)


@router.put("/playlists/{playlist_id}/chapters/order", status_code=204)
def order_chapters(playlist_id: Id, body: OrderInput, ctx: Owner, courses: Courses) -> Response:
    courses.reorder_chapters(playlist_id, body.ids, ctx)
    return NO_CONTENT


@router.put("/chapters/{chapter_id}", response_model=ChapterRead)
def update_chapter(chapter_id: Id, body: ChapterInput, ctx: Owner, courses: Courses) -> ChapterRead:
    return courses.update_chapter(chapter_id, body, ctx)


@router.delete("/chapters/{chapter_id}", status_code=204)
def delete_chapter(chapter_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_chapter(chapter_id, ctx)
    return NO_CONTENT


@router.post("/chapters/{chapter_id}/lectures", response_model=LectureRead, status_code=201)
def add_lecture(chapter_id: Id, body: LectureInput, ctx: Owner, courses: Courses) -> LectureRead:
    return courses.add_lecture(chapter_id, body, ctx)


@router.put("/chapters/{chapter_id}/lectures/order", status_code=204)
def order_lectures(chapter_id: Id, body: OrderInput, ctx: Owner, courses: Courses) -> Response:
    courses.reorder_lectures(chapter_id, body.ids, ctx)
    return NO_CONTENT


@router.put("/lectures/{lecture_id}", response_model=LectureRead)
def update_lecture(lecture_id: Id, body: LectureInput, ctx: Owner, courses: Courses) -> LectureRead:
    return courses.update_lecture(lecture_id, body, ctx)


@router.delete("/lectures/{lecture_id}", status_code=204)
def delete_lecture(lecture_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_lecture(lecture_id, ctx)
    return NO_CONTENT


@router.post("/lectures/{lecture_id}/variants", response_model=VariantRead, status_code=201)
def add_variant(lecture_id: Id, body: VariantInput, ctx: Owner, courses: Courses) -> VariantRead:
    return courses.add_variant(lecture_id, body, ctx)


@router.post("/lectures/{lecture_id}/resources", response_model=ResourceRead, status_code=201)
def add_resource(lecture_id: Id, body: ResourceInput, ctx: Owner, courses: Courses) -> ResourceRead:
    return courses.add_resource(lecture_id, body, ctx)


@router.put("/lectures/{lecture_id}/resources/order", status_code=204)
def order_resources(lecture_id: Id, body: OrderInput, ctx: Owner, courses: Courses) -> Response:
    courses.reorder_resources(lecture_id, body.ids, ctx)
    return NO_CONTENT


@router.put("/variants/{variant_id}", response_model=VariantRead)
def update_variant(variant_id: Id, body: VariantInput, ctx: Owner, courses: Courses) -> VariantRead:
    return courses.update_variant(variant_id, body, ctx)


@router.delete("/variants/{variant_id}", status_code=204)
def delete_variant(variant_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_variant(variant_id, ctx)
    return NO_CONTENT


@router.put("/resources/{resource_id}", response_model=ResourceRead)
def update_resource(
    resource_id: Id, body: ResourceInput, ctx: Owner, courses: Courses
) -> ResourceRead:
    return courses.update_resource(resource_id, body, ctx)


@router.delete("/resources/{resource_id}", status_code=204)
def delete_resource(resource_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete_resource(resource_id, ctx)
    return NO_CONTENT


# --- courses ------------------------------------------------------------------------------------


@router.get("", response_model=CoursePage, dependencies=Read)
def list_courses(
    courses: Courses,
    q: Search = None,
    class_id: Annotated[int | None, Query(ge=1)] = None,
    stream_id: Annotated[int | None, Query(ge=1)] = None,
    subject_id: Annotated[int | None, Query(ge=1)] = None,
    status_filter: Annotated[DisplayStatus | None, Query(alias="status")] = None,
    pricing: Literal["free", "paid"] | None = None,
    launch_from: Annotated[UTCInput | None, Query()] = None,
    launch_to: Annotated[UTCInput | None, Query()] = None,
    sort: SortKey = "created",
    order: Literal["asc", "desc"] = "desc",
    limit: Limit = 25,
    offset: Offset = 0,
) -> CoursePage:
    return courses.list(
        q=q,
        class_id=class_id,
        stream_id=stream_id,
        subject_id=subject_id,
        status=status_filter,
        pricing=pricing,
        launch_from=launch_from,
        launch_to=launch_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=CourseDetail, status_code=201)
def create_course(body: CourseCreate, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.create(body, ctx)


@router.get("/{course_id}", response_model=CourseDetail, dependencies=Read)
def get_course(course_id: Id, courses: Courses) -> CourseDetail:
    return courses.detail(course_id)


@router.put("/{course_id}", response_model=CourseDetail)
def update_course(course_id: Id, body: CourseUpdate, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.update(course_id, body, ctx)


@router.delete("/{course_id}", status_code=204)
def delete_course(course_id: Id, ctx: Owner, courses: Courses) -> Response:
    courses.delete(course_id, ctx)
    return NO_CONTENT


@router.get("/{course_id}/publish-check", response_model=PublishCheck, dependencies=Read)
def publish_check(course_id: Id, courses: Courses) -> PublishCheck:
    return courses.publish_check(course_id)


@router.post("/{course_id}/publish", response_model=CourseDetail)
def publish(course_id: Id, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.publish(course_id, ctx)


@router.post("/{course_id}/unpublish", response_model=CourseDetail)
def unpublish(course_id: Id, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.unpublish(course_id, ctx)


@router.post("/{course_id}/archive", response_model=CourseDetail)
def archive(course_id: Id, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.archive(course_id, ctx)


@router.post("/{course_id}/unarchive", response_model=CourseDetail)
def unarchive(course_id: Id, ctx: Owner, courses: Courses) -> CourseDetail:
    return courses.unarchive(course_id, ctx)


@router.post("/{course_id}/playlists", response_model=PlaylistRead, status_code=201)
def add_playlist(course_id: Id, body: PlaylistInput, ctx: Owner, courses: Courses) -> PlaylistRead:
    return courses.add_playlist(course_id, body, ctx)


@router.put("/{course_id}/playlists/order", status_code=204)
def order_playlists(course_id: Id, body: OrderInput, ctx: Owner, courses: Courses) -> Response:
    courses.reorder_playlists(course_id, body.ids, ctx)
    return NO_CONTENT


# Preview as a student would see it (owner and viewer; drafts and unpublished lectures included).
@router.get("/{course_id}/preview", response_model=StudentCourseDetail, dependencies=Read)
def preview(course_id: Id, enrollments: Enrollments) -> StudentCourseDetail:
    return enrollments.course_for_student(course_id, include_unpublished=True)


@router.get(
    "/{course_id}/preview/lectures/{lecture_id}", response_model=Playback, dependencies=Read
)
def preview_lecture(
    course_id: Id, lecture_id: Id, enrollments: Enrollments, language: Language = None
) -> Playback:
    return enrollments.playback(course_id, lecture_id, None, language, preview=True)


@router.get(
    "/{course_id}/preview/resources/{resource_id}", response_model=DownloadLink, dependencies=Read
)
def preview_resource(course_id: Id, resource_id: Id, enrollments: Enrollments) -> DownloadLink:
    return enrollments.download(course_id, resource_id, None, preview=True)


# --- trusted app backend (COURSE_API_TOKEN) ---------------------------------------------------

Api = Annotated[CourseContext, Depends(course_api_context)]


@integration_router.get("/catalog", response_model=StudentCatalog)
def catalog(
    _: Api, enrollments: Enrollments, student_id: OptionalStudentId = None
) -> StudentCatalog:
    return enrollments.catalog(student_id)


@integration_router.get("/students/{student_id}/enrollments", response_model=list[EnrollmentRead])
def student_enrollments(
    _: Api,
    enrollments: Enrollments,
    student_id: Annotated[str, Path(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")],
) -> list[EnrollmentRead]:
    return enrollments.student_enrollments(student_id)


@integration_router.post("/orders", response_model=EnrollmentResult)
def create_order(body: OrderCreate, ctx: Api, enrollments: Enrollments) -> EnrollmentResult:
    return enrollments.create_order(body, ctx)


@integration_router.post("/payments/confirm", response_model=EnrollmentResult)
def confirm_payment(body: PaymentConfirm, ctx: Api, enrollments: Enrollments) -> EnrollmentResult:
    return enrollments.confirm_payment(body, ctx)


@integration_router.post("/payments/reverse", response_model=EnrollmentRead)
def reverse_payment(body: PaymentReversal, ctx: Api, enrollments: Enrollments) -> EnrollmentRead:
    return enrollments.reverse_payment(body, ctx)


@integration_router.post("/enroll-free", response_model=EnrollmentResult)
def enroll_free(body: FreeEnroll, ctx: Api, enrollments: Enrollments) -> EnrollmentResult:
    return enrollments.enroll_free(body, ctx)


@integration_router.get("/{course_id}", response_model=StudentCourseDetail)
def course_detail(
    course_id: Id, _: Api, enrollments: Enrollments, student_id: OptionalStudentId = None
) -> StudentCourseDetail:
    return enrollments.course_for_student(course_id, student_id)


@integration_router.get("/{course_id}/access", response_model=AccessState)
def access(course_id: Id, _: Api, enrollments: Enrollments, student_id: StudentId) -> AccessState:
    enrollments._visible(course_id)  # noqa: SLF001 - 404 for drafts / unknown courses
    return enrollments.access(course_id, student_id)


@integration_router.get("/{course_id}/lectures/{lecture_id}/play", response_model=Playback)
def play(
    course_id: Id,
    lecture_id: Id,
    _: Api,
    enrollments: Enrollments,
    student_id: OptionalStudentId = None,
    language: Language = None,
) -> Playback:
    return enrollments.playback(course_id, lecture_id, student_id, language)


@integration_router.get(
    "/{course_id}/resources/{resource_id}/download", response_model=DownloadLink
)
def download(
    course_id: Id, resource_id: Id, _: Api, enrollments: Enrollments, student_id: StudentId
) -> DownloadLink:
    return enrollments.download(course_id, resource_id, student_id)
