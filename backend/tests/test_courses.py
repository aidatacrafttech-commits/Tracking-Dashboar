"""Course management: courses, taxonomy, content tree, language variants, files, publishing,
purchases (orders, payment confirmation, duplicate callbacks, free enrollment, refunds),
per-student validity from the purchase date, expiry, access control and permissions."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, update

from app.core.errors import ConflictError
from app.main import create_app
from app.models import AuditLog, CourseEnrollment, EnrollmentEvent
from app.models.enums import Actor, CourseActor, EnrollmentStatus, ValidityUnit
from app.schemas.course import CourseUpdate, PaymentConfirm
from app.services.context import RequestContext
from app.services.course_enrollments import CourseEnrollmentService
from app.services.course_storage import StoredObject
from app.services.course_validity import access_expiry, add_months
from app.services.courses import CourseContext, CourseService
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings

API_TOKEN = "course-api-token-0123456789-abcdefghijklmnopqrs"
C = "/api/v1/courses"
I = "/api/v1/integrations/courses"  # noqa: E741
OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
APP = {"Authorization": f"Bearer {API_TOKEN}"}
IST = "Asia/Kolkata"
_ids = itertools.count(1)
PAST_LAUNCH = "2026-01-01T09:00:00+05:30"


class FakeStorage:
    """Stands in for S3: records upload tickets, 'stores' objects the test puts there."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.tickets: list[tuple[str, str, int]] = []

    def presigned_upload(self, key: str, content_type: str, max_bytes: int) -> dict[str, Any]:
        self.tickets.append((key, content_type, max_bytes))
        return {"url": "https://bucket.s3.ap-south-1.amazonaws.com/", "fields": {"key": key}}

    def head(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def signed_url(self, key: str, *, download_name: str | None = None) -> str:
        return f"https://signed.example/{key}" + (f"?name={download_name}" if download_name else "")


@pytest.fixture
def capp(db_path: Path) -> Iterator[FastAPI]:
    application = create_app(
        make_settings(db_path, course_api_token=SecretStr(API_TOKEN)), start_scheduler=False
    )
    application.state.course_storage = FakeStorage()
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(capp: FastAPI) -> Iterator[TestClient]:
    with TestClient(capp) as c:
        yield c


def ok(resp, status: int = 200) -> Any:  # type: ignore[no-untyped-def]
    assert resp.status_code == status, resp.text
    return resp.json() if resp.content else None


def error(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def term(api: TestClient, kind: str, name: str) -> int:
    return ok(api.post(f"{C}/taxonomy", json={"kind": kind, "name": name}, headers=OWNER), 201)[
        "id"
    ]


def course_body(**over: Any) -> dict[str, Any]:
    return {
        "title": "Class 12 Accountancy",
        "description": "Complete board syllabus with solved papers.",
        "validity_value": 6,
        "validity_unit": "months",
        "price": "999",
        "discounted_price": "799",
        "original_price": "1499",
        "launch_at": PAST_LAUNCH,
        **over,
    }


def make_course(api: TestClient, *, publish: bool = True, **over: Any) -> dict[str, Any]:
    """A complete course: class + subject, one playlist / chapter / lecture with an English
    video URL and a Hindi variant, plus a free-preview lecture."""
    over.setdefault("class_id", term(api, "class", f"Class {next(_ids)}"))
    over.setdefault("subject_id", term(api, "subject", f"Subject {next(_ids)}"))
    course = ok(api.post(C, json=course_body(**over), headers=OWNER), 201)
    playlist = ok(
        api.post(f"{C}/{course['id']}/playlists", json={"title": "Full course"}, headers=OWNER), 201
    )
    chapter = ok(
        api.post(
            f"{C}/playlists/{playlist['id']}/chapters", json={"title": "Chapter 1"}, headers=OWNER
        ),
        201,
    )
    lecture = ok(
        api.post(
            f"{C}/chapters/{chapter['id']}/lectures",
            json={"title": "Journal entries", "status": "published", "duration_seconds": 1800},
            headers=OWNER,
        ),
        201,
    )
    ok(
        api.post(
            f"{C}/lectures/{lecture['id']}/variants",
            json={"language": "en", "video_url": "https://cdn.example.com/journal-en.mp4"},
            headers=OWNER,
        ),
        201,
    )
    ok(
        api.post(
            f"{C}/lectures/{lecture['id']}/variants",
            json={"language": "hi", "video_url": "https://cdn.example.com/journal-hi.mp4"},
            headers=OWNER,
        ),
        201,
    )
    preview = ok(
        api.post(
            f"{C}/chapters/{chapter['id']}/lectures",
            json={"title": "Introduction", "status": "published", "is_free_preview": True},
            headers=OWNER,
        ),
        201,
    )
    ok(
        api.post(
            f"{C}/lectures/{preview['id']}/variants",
            json={"language": "en", "video_url": "https://cdn.example.com/intro.mp4"},
            headers=OWNER,
        ),
        201,
    )
    if publish:
        ok(api.post(f"{C}/{course['id']}/publish", headers=OWNER))
    detail = ok(api.get(f"{C}/{course['id']}", headers=OWNER))
    detail["lecture_id"], detail["preview_lecture_id"] = lecture["id"], preview["id"]
    detail["chapter_id"], detail["playlist_id"] = chapter["id"], playlist["id"]
    return detail


def pay(api: TestClient, course_id: int, student: str, **over: Any):  # type: ignore[no-untyped-def]
    body = {
        "course_id": course_id,
        "student_id": student,
        "student_name": "Asha Verma",
        "payment_id": f"pay_{next(_ids)}",
        "payment_provider": "razorpay",
        "amount_paid": "799",
        **over,
    }
    return api.post(f"{I}/payments/confirm", json=body, headers=APP)


def play(
    api: TestClient,
    course_id: int,
    lecture_id: int,
    student: str | None,
    language: str | None = None,
):  # type: ignore[no-untyped-def]
    params = {k: v for k, v in (("student_id", student), ("language", language)) if v}
    return api.get(f"{I}/{course_id}/lectures/{lecture_id}/play", params=params, headers=APP)


# --- 1. course creation and editing ----------------------------------------------------------


def test_create_and_edit_course_with_pricing_validation(api: TestClient, capp: FastAPI) -> None:
    class_id, subject_id = term(api, "class", "Class 12"), term(api, "subject", "Accountancy")
    stream_id = term(api, "stream", "Commerce")
    c = ok(
        api.post(
            C,
            json=course_body(class_id=class_id, stream_id=stream_id, subject_id=subject_id),
            headers=OWNER,
        ),
        201,
    )
    assert (c["status"], c["display_status"], c["version"]) == ("draft", "draft", 1)
    assert (c["class_name"], c["stream_name"], c["subject_name"]) == (
        "Class 12",
        "Commerce",
        "Accountancy",
    )
    assert (c["price"], c["discounted_price"], c["effective_price"]) == ("999", "799", "799")
    assert c["validity_label"] == "6 months" and c["can_delete"] is True
    assert "end" not in " ".join(c.keys())  # there is no course end date

    edit = {
        **course_body(class_id=class_id, subject_id=subject_id),
        "title": "Accountancy 2027",
        "validity_value": 1,
        "validity_unit": "years",
        "version": c["version"],
    }
    updated = ok(api.put(f"{C}/{c['id']}", json=edit, headers=OWNER))
    assert (updated["title"], updated["validity_label"], updated["version"]) == (
        "Accountancy 2027",
        "1 year",
        2,
    )
    stale = api.put(f"{C}/{c['id']}", json=edit, headers=OWNER)
    assert stale.status_code == 409 and error(stale) == "concurrent_modification"

    for bad in (
        {"price": None},  # paid course without a price
        {"price": "0"},
        {"discounted_price": "999"},  # not below the price
        {"original_price": "500"},  # MRP below the price
        {"is_free": True},  # free course with prices
        {"price": 99.5},  # floats refused
        {"validity_value": 0},
        {"validity_value": 121},  # months max 120
        {"validity_unit": "weeks"},
        {"launch_at": "2026-11-01T09:00:00"},  # no timezone
        {"class_id": subject_id},  # a subject used as a class
    ):
        resp = api.post(
            C, json={**course_body(class_id=class_id, subject_id=subject_id), **bad}, headers=OWNER
        )
        assert resp.status_code == 422, (bad, resp.text)
    free = ok(
        api.post(
            C,
            json=course_body(is_free=True, price=None, discounted_price=None, original_price=None),
            headers=OWNER,
        ),
        201,
    )
    assert (free["is_free"], free["effective_price"], free["price"]) == (True, "0", None)
    with capp.state.session_factory() as s:
        assert "course.create" in s.scalars(select(AuditLog.action)).all()


def test_taxonomy_is_managed_and_protected(api: TestClient) -> None:
    class_id = term(api, "class", "Class 11")
    dup = api.post(f"{C}/taxonomy", json={"kind": "class", "name": "class 11"}, headers=OWNER)
    assert dup.status_code == 409 and error(dup) == "duplicate_taxonomy"
    assert (
        api.post(
            f"{C}/taxonomy", json={"kind": "subject", "name": "Class 11"}, headers=OWNER
        ).status_code
        == 201
    )
    make_course(api, class_id=class_id, publish=False)
    used = api.delete(f"{C}/taxonomy/{class_id}", headers=OWNER)
    assert used.status_code == 409 and error(used) == "taxonomy_in_use"
    terms = ok(api.get(f"{C}/taxonomy", params={"kind": "class"}, headers=VIEWER))
    assert next(t for t in terms if t["id"] == class_id)["course_count"] == 1
    ok(api.put(f"{C}/taxonomy/{class_id}", json={"is_active": False}, headers=OWNER))


# --- 2. launch date and 3. publishing ----------------------------------------------------------


def test_launch_date_controls_purchase_not_validity(api: TestClient) -> None:
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    upcoming = make_course(api, launch_at=future)
    assert upcoming["display_status"] == "scheduled"
    early = pay(api, upcoming["id"], "early-bird")
    assert early.status_code == 409 and error(early) == "course_not_launched"
    order = api.post(
        f"{I}/orders",
        json={
            "course_id": upcoming["id"],
            "student_id": "s1",
            "order_id": "ord_up",
            "payment_provider": "razorpay",
        },
        headers=APP,
    )
    assert order.status_code == 409 and error(order) == "course_not_launched"
    catalog = ok(api.get(f"{I}/catalog", headers=APP))["items"]
    item = next(i for i in catalog if i["id"] == upcoming["id"])
    assert item["availability"] == {
        "purchasable": False,
        "reason": "not_launched",
        "launch_at": item["availability"]["launch_at"],
    }

    launched = make_course(api)
    assert launched["display_status"] == "published"
    result = ok(pay(api, launched["id"], "s2"))["enrollment"]
    # Validity counts from this student's purchase, not from the launch date (1 Jan 2026).
    start = datetime.fromisoformat(result["access_start_at"])
    assert abs(start - datetime.now(UTC)) < timedelta(minutes=1)
    assert datetime.fromisoformat(result["access_expires_at"]) == access_expiry(
        start, 6, ValidityUnit.MONTHS, IST
    )


def test_publish_requires_complete_course(api: TestClient) -> None:
    bare = ok(api.post(C, json=course_body(description=None, launch_at=None), headers=OWNER), 201)
    check = ok(api.get(f"{C}/{bare['id']}/publish-check", headers=VIEWER))
    codes = {p["code"] for p in check["problems"] if p["blocking"]}
    assert check["ready"] is False
    assert {
        "missing_description",
        "missing_class",
        "missing_subject",
        "missing_launch_date",
        "no_playlists",
    } <= codes
    refused = api.post(f"{C}/{bare['id']}/publish", headers=OWNER)
    assert refused.status_code == 409 and error(refused) == "publish_requirements"

    # A published lecture without any video blocks publishing
    c = make_course(api, publish=False)
    lonely = ok(
        api.post(
            f"{C}/chapters/{c['chapter_id']}/lectures",
            json={"title": "No video yet", "status": "published"},
            headers=OWNER,
        ),
        201,
    )
    blocked = api.post(f"{C}/{c['id']}/publish", headers=OWNER)
    assert blocked.status_code == 409 and "No video yet" in blocked.json()["error"]["message"]
    ok(
        api.put(
            f"{C}/lectures/{lonely['id']}",
            json={"title": "No video yet", "status": "draft"},
            headers=OWNER,
        )
    )
    published = ok(api.post(f"{C}/{c['id']}/publish", headers=OWNER))
    assert published["status"] == "published" and published["published_at"]
    # A published course must stay complete: its only playable lectures can't be removed
    ok(api.delete(f"{C}/lectures/{c['preview_lecture_id']}", headers=OWNER), 204)
    last = api.delete(f"{C}/lectures/{c['lecture_id']}", headers=OWNER)
    assert last.status_code == 409 and error(last) == "publish_requirements"


def test_status_transitions_and_archive_keeps_existing_access(api: TestClient) -> None:
    c = make_course(api)
    ok(pay(api, c["id"], "keeper"))
    unpublish = api.post(f"{C}/{c['id']}/unpublish", headers=OWNER)
    assert unpublish.status_code == 409 and error(unpublish) == "course_has_enrollments"
    delete = api.delete(f"{C}/{c['id']}", headers=OWNER)
    assert delete.status_code == 409 and error(delete) == "course_has_enrollments"
    archived = ok(api.post(f"{C}/{c['id']}/archive", headers=OWNER))
    assert archived["display_status"] == "archived"
    refused = pay(api, c["id"], "newcomer")
    assert refused.status_code == 409 and error(refused) == "course_archived"
    # The existing student keeps access according to their own validity
    assert ok(play(api, c["id"], c["lecture_id"], "keeper"))["language"] == "en"
    assert (
        ok(api.get(f"{I}/{c['id']}", params={"student_id": "keeper"}, headers=APP))["availability"][
            "reason"
        ]
        == "archived"
    )
    assert c["id"] not in [i["id"] for i in ok(api.get(f"{I}/catalog", headers=APP))["items"]]
    restored = ok(api.post(f"{C}/{c['id']}/unarchive", headers=OWNER))
    assert restored["status"] == "draft"
    draft = make_course(api, publish=False)
    assert ok(api.delete(f"{C}/{draft['id']}", headers=OWNER), 204) is None


# --- 4. content management and 5. language variants -----------------------------------------


def test_content_tree_crud_and_reorder(api: TestClient) -> None:
    c = make_course(api, publish=False)
    p2 = ok(api.post(f"{C}/{c['id']}/playlists", json={"title": "Revision"}, headers=OWNER), 201)
    ok(
        api.put(
            f"{C}/{c['id']}/playlists/order",
            json={"ids": [p2["id"], c["playlist_id"]]},
            headers=OWNER,
        ),
        204,
    )
    detail = ok(api.get(f"{C}/{c['id']}", headers=OWNER))
    assert [p["title"] for p in detail["playlists"]] == ["Revision", "Full course"]
    bad = api.put(f"{C}/{c['id']}/playlists/order", json={"ids": [p2["id"]]}, headers=OWNER)
    assert bad.status_code == 422 and error(bad) == "invalid_order"

    lectures = [l["id"] for l in detail["playlists"][1]["chapters"][0]["lectures"]]  # noqa: E741
    ok(
        api.put(
            f"{C}/chapters/{c['chapter_id']}/lectures/order",
            json={"ids": lectures[::-1]},
            headers=OWNER,
        ),
        204,
    )
    detail = ok(api.get(f"{C}/{c['id']}", headers=OWNER))
    assert [l["id"] for l in detail["playlists"][1]["chapters"][0]["lectures"]] == lectures[::-1]  # noqa: E741

    ch2 = ok(
        api.post(f"{C}/playlists/{p2['id']}/chapters", json={"title": "Mock tests"}, headers=OWNER),
        201,
    )
    ok(
        api.put(
            f"{C}/chapters/{ch2['id']}",
            json={"title": "Mock tests (2027)", "description": "Timed"},
            headers=OWNER,
        )
    )
    ok(api.put(f"{C}/playlists/{p2['id']}", json={"title": "Revision & mocks"}, headers=OWNER))
    ok(api.delete(f"{C}/chapters/{ch2['id']}", headers=OWNER), 204)
    ok(api.delete(f"{C}/playlists/{p2['id']}", headers=OWNER), 204)
    detail = ok(api.get(f"{C}/{c['id']}", headers=OWNER))
    assert [p["title"] for p in detail["playlists"]] == ["Full course"]
    assert detail["lecture_count"] == 2 and detail["playlist_count"] == 1
    # Preview shows the course as students will see it, drafts included
    preview = ok(api.get(f"{C}/{c['id']}/preview", headers=VIEWER))
    assert preview["playlists"][0]["chapters"][0]["lectures"][0]["languages"]
    assert ok(api.get(f"{C}/{c['id']}/preview/lectures/{c['lecture_id']}", headers=VIEWER))[
        "video_url"
    ]


def test_language_variants(api: TestClient) -> None:
    c = make_course(api)
    lecture = c["lecture_id"]
    dup = api.post(
        f"{C}/lectures/{lecture}/variants",
        json={"language": "hi", "video_url": "https://cdn.example.com/x.mp4"},
        headers=OWNER,
    )
    assert dup.status_code == 422 and error(dup) == "duplicate_language"
    for bad in (
        {"language": "xx", "video_url": "https://a.example/v.mp4"},
        {"language": "ta"},  # no video
        {"language": "ta", "video_url": "http://insecure.example/v.mp4"},
    ):
        assert (
            api.post(f"{C}/lectures/{lecture}/variants", json=bad, headers=OWNER).status_code == 422
        )
    tamil = ok(
        api.post(
            f"{C}/lectures/{lecture}/variants",
            json={
                "language": "ta",
                "video_url": "https://cdn.example.com/ta.mp4",
                "description": "Tamil narration",
            },
            headers=OWNER,
        ),
        201,
    )
    assert tamil["language_name"] == "Tamil"
    ok(pay(api, c["id"], "linguist"))
    assert ok(play(api, c["id"], lecture, "linguist"))["language"] == "en"  # default = first added
    hindi = ok(play(api, c["id"], lecture, "linguist", "hi"))
    assert hindi["video_url"].endswith("journal-hi.mp4")
    assert {a["code"] for a in hindi["available_languages"]} == {"en", "hi", "ta"}
    missing = play(api, c["id"], lecture, "linguist", "mr")
    assert missing.status_code == 404 and error(missing) == "language_unavailable"
    assert (
        "Marathi" in missing.json()["error"]["message"]
        and "Tamil" in missing.json()["error"]["message"]
    )
    assert {a["name"] for a in missing.json()["error"]["details"]["available_languages"]} == {
        "English",
        "Hindi",
        "Tamil",
    }
    # Removing the default language moves the default to another variant
    english = next(
        v
        for v in ok(api.get(f"{C}/{c['id']}", headers=OWNER))["playlists"][0]["chapters"][0][
            "lectures"
        ]
        if v["id"] == lecture
    )["variants"][0]
    ok(api.delete(f"{C}/variants/{english['id']}", headers=OWNER), 204)
    assert ok(play(api, c["id"], lecture, "linguist"))["language"] == "hi"


def test_file_uploads_are_validated_and_verified(api: TestClient, capp: FastAPI) -> None:
    storage: FakeStorage = capp.state.course_storage
    for bad, code in (
        (
            {"kind": "image", "filename": "a.gif", "content_type": "image/gif", "size_bytes": 10},
            "invalid_file_type",
        ),
        (
            {"kind": "image", "filename": "a.png", "content_type": "image/jpeg", "size_bytes": 10},
            "invalid_file_type",
        ),
        (
            {
                "kind": "document",
                "filename": "n.pdf",
                "content_type": "application/pdf",
                "size_bytes": 101 * 1024 * 1024,
            },
            "file_too_large",
        ),
    ):
        resp = api.post(f"{C}/assets/uploads", json=bad, headers=OWNER)
        assert resp.status_code == 422 and error(resp) == code, resp.text
    ticket = ok(
        api.post(
            f"{C}/assets/uploads",
            json={
                "kind": "video",
                "filename": "../../etc/Lecture 1.MP4",
                "content_type": "video/mp4",
                "size_bytes": 5000,
            },
            headers=OWNER,
        ),
        201,
    )
    key, content_type, max_bytes = storage.tickets[-1]
    assert key.startswith("courses/video/") and key.endswith(".mp4") and ".." not in key
    assert (content_type, max_bytes) == ("video/mp4", 5000)
    assert ticket["asset"]["status"] == "pending" and ticket["asset"]["filename"] == "Lecture 1.MP4"
    asset_id = ticket["asset"]["id"]
    missing = api.post(f"{C}/assets/{asset_id}/complete", headers=OWNER)
    assert missing.status_code == 409 and error(missing) == "upload_missing"
    storage.objects[key] = StoredObject(4999, "video/mp4")
    assert error(api.post(f"{C}/assets/{asset_id}/complete", headers=OWNER)) == "upload_mismatch"
    storage.objects[key] = StoredObject(5000, "video/mp4")
    done = ok(api.post(f"{C}/assets/{asset_id}/complete", headers=OWNER))
    assert done["status"] == "uploaded" and done["url"].startswith("https://signed.example/")
    # A pending or wrong-kind asset can't be attached
    c = make_course(api)
    wrong = api.post(
        f"{C}/lectures/{c['lecture_id']}/resources",
        json={"title": "Notes", "asset_id": asset_id},
        headers=OWNER,
    )
    assert wrong.status_code == 422 and error(wrong) == "invalid_asset"
    ok(
        api.post(
            f"{C}/lectures/{c['lecture_id']}/variants",
            json={"language": "mr", "video_asset_id": asset_id},
            headers=OWNER,
        ),
        201,
    )
    ok(pay(api, c["id"], "viewer-1"))
    marathi = ok(play(api, c["id"], c["lecture_id"], "viewer-1", "mr"))
    assert (
        marathi["video_url"] == f"https://signed.example/{key}" and marathi["url_expires_in"] == 900
    )
    # Storage not configured -> uploads refused clearly
    capp.state.course_storage = None
    off = api.post(
        f"{C}/assets/uploads",
        json={"kind": "image", "filename": "a.png", "content_type": "image/png", "size_bytes": 10},
        headers=OWNER,
    )
    assert off.status_code == 503 and error(off) == "storage_not_configured"
    assert ok(api.get(f"{C}/storage", headers=VIEWER))["configured"] is False


def test_study_materials_download_rules(api: TestClient, capp: FastAPI) -> None:
    storage: FakeStorage = capp.state.course_storage
    ids = []
    for name in ("notes.pdf", "answers.pdf"):
        t = ok(
            api.post(
                f"{C}/assets/uploads",
                json={
                    "kind": "document",
                    "filename": name,
                    "content_type": "application/pdf",
                    "size_bytes": 100,
                },
                headers=OWNER,
            ),
            201,
        )
        storage.objects[storage.tickets[-1][0]] = StoredObject(100, "application/pdf")
        ok(api.post(f"{C}/assets/{t['asset']['id']}/complete", headers=OWNER))
        ids.append(t["asset"]["id"])
    c = make_course(api)
    notes = ok(
        api.post(
            f"{C}/lectures/{c['lecture_id']}/resources",
            json={"title": "Notes", "asset_id": ids[0]},
            headers=OWNER,
        ),
        201,
    )
    locked = ok(
        api.post(
            f"{C}/lectures/{c['lecture_id']}/resources",
            json={"title": "Answer key", "asset_id": ids[1], "is_downloadable": False},
            headers=OWNER,
        ),
        201,
    )
    url = f"{I}/{c['id']}/resources/{notes['id']}/download"
    assert error(api.get(url, params={"student_id": "stranger"}, headers=APP)) == "not_enrolled"
    ok(pay(api, c["id"], "reader"))
    link = ok(api.get(url, params={"student_id": "reader"}, headers=APP))
    assert link["url"].endswith("?name=notes.pdf")
    denied = api.get(
        f"{I}/{c['id']}/resources/{locked['id']}/download",
        params={"student_id": "reader"},
        headers=APP,
    )
    assert denied.status_code == 403 and error(denied) == "download_not_permitted"


# --- 6. paid purchase, 7. free enrollment, 8. duplicate webhooks ----------------------------------


def test_paid_purchase_from_order_to_access(api: TestClient, capp: FastAPI) -> None:
    c = make_course(api)
    order = ok(
        api.post(
            f"{I}/orders",
            json={
                "course_id": c["id"],
                "student_id": "buyer",
                "student_name": "Ravi",
                "order_id": "order_1",
                "payment_provider": "razorpay",
            },
            headers=APP,
        )
    )
    assert order["enrollment"]["status"] == "pending" and order["duplicate"] is False
    again = ok(
        api.post(
            f"{I}/orders",
            json={
                "course_id": c["id"],
                "student_id": "buyer",
                "order_id": "order_1",
                "payment_provider": "razorpay",
            },
            headers=APP,
        )
    )
    assert again["duplicate"] is True and again["enrollment"]["id"] == order["enrollment"]["id"]
    pending = play(api, c["id"], c["lecture_id"], "buyer")
    assert pending.status_code == 403 and error(pending) == "enrollment_pending"
    assert (
        ok(api.get(f"{I}/{c['id']}/access", params={"student_id": "buyer"}, headers=APP))["status"]
        == "pending"
    )

    for bad, code in (
        (dict(amount_paid="800"), "amount_mismatch"),
        (dict(amount_paid="500"), "amount_mismatch"),
        (dict(currency="USD"), "currency_mismatch"),
    ):
        resp = pay(api, c["id"], "buyer", order_id="order_1", **bad)
        assert resp.status_code == 422 and error(resp) == code
    confirmed = ok(
        pay(
            api,
            c["id"],
            "buyer",
            order_id="order_1",
            payment_id="pay_A",
            amount_paid="599",
            voucher_code="save200",
        )
    )
    e = confirmed["enrollment"]
    assert (e["id"], e["status"], e["amount_paid"], e["list_price"], e["voucher_code"]) == (
        order["enrollment"]["id"],
        "active",
        "599",
        "799",
        "SAVE200",
    )
    assert e["validity_label"] == "6 months" and e["purchased_at"] == e["access_start_at"]
    access = ok(api.get(f"{I}/{c['id']}/access", params={"student_id": "buyer"}, headers=APP))
    assert access["status"] == "active" and "access until" in access["message"]
    assert (
        ok(play(api, c["id"], c["lecture_id"], "buyer"))["access_expires_at"]
        == e["access_expires_at"]
    )
    detail = ok(api.get(f"{C}/enrollments/{e['id']}", headers=VIEWER))
    assert [ev["event"] for ev in detail["events"]] == ["order_created", "payment_confirmed"]
    summary = ok(api.get(f"{C}/summary", headers=VIEWER))
    assert summary["active_enrollments"] == 1 and summary["revenue"] == [
        {"currency": "INR", "amount": "599"}
    ]
    mine = ok(api.get(f"{I}/students/buyer/enrollments", headers=APP))
    assert [m["status"] for m in mine] == ["active"]


def test_free_course_enrollment(api: TestClient) -> None:
    c = make_course(api, is_free=True, price=None, discounted_price=None, original_price=None)
    paid_attempt = pay(api, c["id"], "learner", amount_paid="0")
    assert paid_attempt.status_code == 409 and error(paid_attempt) == "course_is_free"
    first = ok(
        api.post(
            f"{I}/enroll-free", json={"course_id": c["id"], "student_id": "learner"}, headers=APP
        )
    )
    e = first["enrollment"]
    assert (e["status"], e["source"], e["amount_paid"], e["payment_id"]) == (
        "active",
        "free",
        "0",
        None,
    )
    again = ok(
        api.post(
            f"{I}/enroll-free", json={"course_id": c["id"], "student_id": "learner"}, headers=APP
        )
    )
    assert again["duplicate"] is True and again["enrollment"]["id"] == e["id"]
    assert ok(play(api, c["id"], c["lecture_id"], "learner"))["video_url"]
    paid = make_course(api)
    refused = api.post(
        f"{I}/enroll-free", json={"course_id": paid["id"], "student_id": "learner"}, headers=APP
    )
    assert refused.status_code == 409 and error(refused) == "course_is_paid"


def test_duplicate_payment_callbacks_create_one_enrollment(api: TestClient, capp: FastAPI) -> None:
    c = make_course(api)
    first = ok(pay(api, c["id"], "dup", payment_id="pay_same"))
    second = ok(pay(api, c["id"], "dup", payment_id="pay_same"))
    assert second["duplicate"] is True and second["enrollment"]["id"] == first["enrollment"]["id"]
    clash = pay(api, c["id"], "someone-else", payment_id="pay_same")
    assert clash.status_code == 409 and error(clash) == "payment_conflict"

    # The same webhook delivered 12 times at once still yields exactly one enrollment
    settings = capp.state.settings
    ctx = CourseContext(CourseActor.COURSE_API, RequestContext(actor=Actor.SYSTEM))
    body = PaymentConfirm(
        course_id=c["id"],
        student_id="racer",
        payment_id="pay_race",
        payment_provider="razorpay",
        amount_paid="799",
    )

    def confirm(_: int) -> bool:
        with capp.state.session_factory() as s:
            return (
                CourseEnrollmentService(s, settings, None, IST).confirm_payment(body, ctx).duplicate
            )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(confirm, range(12)))
    assert results.count(False) == 1 and results.count(True) == 11
    with capp.state.session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(CourseEnrollment)
                .where(CourseEnrollment.payment_id == "pay_race")
            )
            == 1
        )


def test_refund_ends_access_but_keeps_history(api: TestClient) -> None:
    c = make_course(api)
    e = ok(pay(api, c["id"], "refundee", payment_id="pay_R"))["enrollment"]
    refunded = ok(
        api.post(
            f"{I}/payments/reverse",
            json={"payment_id": "pay_R", "status": "refunded", "reason": "customer request"},
            headers=APP,
        )
    )
    assert refunded["status"] == "refunded"
    denied = play(api, c["id"], c["lecture_id"], "refundee")
    assert denied.status_code == 403 and error(denied) == "enrollment_refunded"
    detail = ok(api.get(f"{C}/enrollments/{e['id']}", headers=OWNER))
    assert [ev["event"] for ev in detail["events"]] == ["payment_confirmed", "refunded"]
    assert ok(api.get(f"{C}/summary", headers=OWNER))["revenue"] == []


# --- 9-11. validity calculation ------------------------------------------------------------------


def test_validity_uses_calendar_months_from_each_purchase() -> None:
    ist = lambda *a: datetime(*a, tzinfo=__import__("zoneinfo").ZoneInfo(IST))  # noqa: E731
    # Student A and B buy the same 6-month course on different days
    a = access_expiry(ist(2026, 11, 10, 10, 0), 6, ValidityUnit.MONTHS, IST)
    b = access_expiry(ist(2026, 12, 20, 18, 30), 6, ValidityUnit.MONTHS, IST)
    assert a.astimezone(__import__("zoneinfo").ZoneInfo(IST)) == ist(2027, 5, 10, 10, 0)
    assert b.astimezone(__import__("zoneinfo").ZoneInfo(IST)) == ist(2027, 6, 20, 18, 30)
    assert a.tzinfo == UTC
    # Month-end: 31 Jan + 1 month -> last day of February (leap and non-leap)
    assert add_months(ist(2027, 1, 31, 12), 1) == ist(2027, 2, 28, 12)
    assert add_months(ist(2028, 1, 31, 12), 1) == ist(2028, 2, 29, 12)
    assert add_months(ist(2026, 8, 31), 6) == ist(2027, 2, 28)
    assert add_months(ist(2026, 10, 31), 7) == ist(2027, 5, 31)
    # One year is one calendar year; 29 Feb + 1 year -> 28 Feb
    assert access_expiry(ist(2026, 11, 10, 9, 0), 1, ValidityUnit.YEARS, IST) == ist(
        2027, 11, 10, 9, 0
    )
    assert access_expiry(ist(2028, 2, 29, 9, 0), 1, ValidityUnit.YEARS, IST) == ist(
        2029, 2, 28, 9, 0
    )
    assert access_expiry(ist(2026, 11, 10, 9, 0), 10, ValidityUnit.MONTHS, IST) == ist(
        2027, 9, 10, 9, 0
    )
    assert access_expiry(ist(2026, 11, 10, 9, 0), 45, ValidityUnit.DAYS, IST) == ist(
        2026, 12, 25, 9, 0
    )
    # The local date is what counts: 1 a.m. IST on 10 Nov is still 9 Nov in UTC
    assert access_expiry(ist(2026, 11, 10, 1, 0), 6, ValidityUnit.MONTHS, IST) == ist(
        2027, 5, 10, 1, 0
    )
    with pytest.raises(ValueError):
        access_expiry(ist(2026, 1, 1), 11, ValidityUnit.YEARS, IST)


def test_purchase_example_from_the_requirements(api: TestClient, capp: FastAPI) -> None:
    """Launch 1 Nov 2026, 6 months validity; A buys 10 Nov, B buys 20 Dec."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    c = make_course(api, launch_at="2026-11-01T00:00:00+05:30")
    settings, ctx = (
        capp.state.settings,
        CourseContext(CourseActor.COURSE_API, RequestContext(actor=Actor.SYSTEM)),
    )
    results = {}
    for student, paid in (
        ("A", datetime(2026, 11, 10, 11, 0, tzinfo=ZoneInfo(IST))),
        ("B", datetime(2026, 12, 20, 11, 0, tzinfo=ZoneInfo(IST))),
    ):
        with capp.state.session_factory() as s:
            service = CourseEnrollmentService(
                s, settings, None, IST, clock=lambda p=paid: p.astimezone(UTC)
            )
            body = PaymentConfirm(
                course_id=c["id"],
                student_id=student,
                payment_id=f"pay_{student}",
                payment_provider="razorpay",
                amount_paid="799",
                paid_at=paid,
            )
            results[student] = service.confirm_payment(body, ctx).enrollment
    local = lambda d: d.astimezone(ZoneInfo(IST)).date().isoformat()  # noqa: E731
    assert (local(results["A"].access_start_at), local(results["A"].access_expires_at)) == (
        "2026-11-10",
        "2027-05-10",
    )
    assert (local(results["B"].access_start_at), local(results["B"].access_expires_at)) == (
        "2026-12-20",
        "2027-06-20",
    )


# --- 12. existing enrollments keep their validity ------------------------------------------


def test_changing_course_validity_never_changes_existing_enrollments(
    api: TestClient, capp: FastAPI
) -> None:
    c = make_course(api)
    before = ok(pay(api, c["id"], "early"))["enrollment"]
    assert before["validity_label"] == "6 months"
    detail = ok(api.get(f"{C}/{c['id']}", headers=OWNER))
    edit = {
        **course_body(class_id=detail["class_id"], subject_id=detail["subject_id"]),
        "validity_value": 1,
        "validity_unit": "years",
        "price": "1999",
        "discounted_price": None,
        "original_price": None,
        "version": detail["version"],
    }
    ok(api.put(f"{C}/{c['id']}", json=edit, headers=OWNER))
    after = ok(api.get(f"{C}/enrollments/{before['id']}", headers=OWNER))
    assert (after["access_expires_at"], after["validity_label"], after["list_price"]) == (
        before["access_expires_at"],
        "6 months",
        "799",
    )
    late = ok(pay(api, c["id"], "late", amount_paid="1999"))["enrollment"]
    assert late["validity_label"] == "1 year" and late["list_price"] == "1999"
    start = datetime.fromisoformat(late["access_start_at"])
    assert datetime.fromisoformat(late["access_expires_at"]) == access_expiry(
        start, 1, ValidityUnit.YEARS, IST
    )
    # Same through the service directly: an update never writes to enrollments
    with capp.state.session_factory() as s:
        rows = s.scalars(select(CourseEnrollment).order_by(CourseEnrollment.id)).all()
        assert [(r.validity_value, r.validity_unit) for r in rows] == [
            (6, ValidityUnit.MONTHS),
            (1, ValidityUnit.YEARS),
        ]


# --- 13. expiry and 14. unauthorised access -----------------------------------------------------


def test_expired_access_is_refused_and_recorded(api: TestClient, capp: FastAPI) -> None:
    c = make_course(api)
    e = ok(pay(api, c["id"], "lapsed"))["enrollment"]
    with capp.state.session_factory() as s, s.begin():
        s.execute(
            update(CourseEnrollment)
            .where(CourseEnrollment.id == e["id"])
            .values(
                access_start_at=datetime.now(UTC) - timedelta(days=200),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
    denied = play(api, c["id"], c["lecture_id"], "lapsed")
    assert denied.status_code == 403 and error(denied) == "course_access_expired"
    assert "expired on" in denied.json()["error"]["message"]
    access = ok(api.get(f"{I}/{c['id']}/access", params={"student_id": "lapsed"}, headers=APP))
    assert access["status"] == "expired"
    listing = ok(api.get(f"{C}/enrollments", params={"status": "expired"}, headers=OWNER))
    assert [i["id"] for i in listing["items"]] == [e["id"]]
    with capp.state.session_factory() as s:
        row = s.get(CourseEnrollment, e["id"])
        assert (
            row.status is EnrollmentStatus.EXPIRED and row.payment_id == e["payment_id"]
        )  # history kept
        assert [
            ev.event
            for ev in s.scalars(
                select(EnrollmentEvent).where(EnrollmentEvent.enrollment_id == e["id"])
            )
        ] == ["payment_confirmed", "expired"]
    # The free preview is still open to everyone
    assert ok(play(api, c["id"], c["preview_lecture_id"], "lapsed"))["free_preview"] is True
    # Buying again starts a new validity from the new purchase
    renewed = ok(pay(api, c["id"], "lapsed"))["enrollment"]
    assert renewed["id"] != e["id"] and renewed["status"] == "active"
    assert ok(play(api, c["id"], c["lecture_id"], "lapsed"))["language"] == "en"


def test_students_without_access_are_refused(api: TestClient) -> None:
    c = make_course(api)
    ok(pay(api, c["id"], "owner-of-it"))
    for student, code in (("stranger", "not_enrolled"), (None, "not_enrolled")):
        resp = play(api, c["id"], c["lecture_id"], student)
        assert resp.status_code == 403 and error(resp) == code
    preview = ok(play(api, c["id"], c["preview_lecture_id"], None))  # free preview: no enrollment
    assert preview["free_preview"] is True
    draft = make_course(api, publish=False)
    assert error(play(api, draft["id"], draft["lecture_id"], "owner-of-it")) == "course_not_found"
    assert error(api.get(f"{I}/{draft['id']}", headers=APP)) == "course_not_found"
    other = make_course(api)
    cross = play(api, other["id"], c["lecture_id"], "owner-of-it")  # lecture of another course
    assert cross.status_code == 404
    # Cancelling (an explicit admin action) ends access
    enrollment = ok(api.get(f"{I}/students/owner-of-it/enrollments", headers=APP))[0]
    ok(
        api.post(
            f"{C}/enrollments/{enrollment['id']}/cancel",
            json={"reason": "chargeback"},
            headers=OWNER,
        )
    )
    assert error(play(api, c["id"], c["lecture_id"], "owner-of-it")) == "enrollment_cancelled"


# --- 15. permissions -----------------------------------------------------------------------------


def test_admin_and_integration_permissions(api: TestClient) -> None:
    c = make_course(api, publish=False)
    # Viewer: read-only
    assert api.get(C, headers=VIEWER).status_code == 200
    assert api.get(f"{C}/enrollments", headers=VIEWER).status_code == 200
    for method, path, body in (
        ("post", C, course_body()),
        ("post", f"{C}/{c['id']}/publish", None),
        ("delete", f"{C}/{c['id']}", None),
        ("post", f"{C}/taxonomy", {"kind": "class", "name": "X"}),
        (
            "post",
            f"{C}/assets/uploads",
            {"kind": "image", "filename": "a.png", "content_type": "image/png", "size_bytes": 1},
        ),
    ):
        assert api.request(method.upper(), path, json=body, headers=VIEWER).status_code == 403, path
    # No credentials
    assert api.get(C).status_code == 401
    # The app token reaches only the integration API
    assert api.get(C, headers=APP).status_code == 401
    assert api.get(f"{I}/catalog", headers=OWNER).status_code == 401
    assert (
        api.get(f"{I}/catalog", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    )
    assert api.get(f"{I}/catalog", headers=APP).status_code == 200


def test_course_api_is_off_without_a_token(client: TestClient) -> None:
    resp = client.get(f"{I}/catalog", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 401 and error(resp) == "course_api_disabled"


def test_course_service_update_ignores_enrollments(capp: FastAPI, api: TestClient) -> None:
    """Directly at the service level: updating any course field leaves enrollment rows as-is."""
    c = make_course(api)
    ok(pay(api, c["id"], "snap"))
    ctx = CourseContext(CourseActor.OWNER, RequestContext(actor=Actor.OWNER))
    with capp.state.session_factory() as s:
        before = [
            (r.validity_value, r.access_expires_at, r.list_price)
            for r in s.scalars(select(CourseEnrollment))
        ]
        detail = CourseService(s, capp.state.settings, None).detail(c["id"])
        body = CourseUpdate(
            **{
                **course_body(class_id=detail.class_id, subject_id=detail.subject_id),
                "validity_value": 10,
                "price": "2999",
                "discounted_price": None,
                "original_price": None,
                "version": detail.version,
            }
        )
        CourseService(s, capp.state.settings, None).update(c["id"], body, ctx)
    with capp.state.session_factory() as s:
        after = [
            (r.validity_value, r.access_expires_at, r.list_price)
            for r in s.scalars(select(CourseEnrollment))
        ]
    assert after == before and before[0][2] == Decimal("799")
    with pytest.raises(ConflictError), capp.state.session_factory() as s:  # stale version refused
        CourseService(s, capp.state.settings, None).update(c["id"], body, ctx)
