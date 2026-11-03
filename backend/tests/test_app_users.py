"""App Users: read-only analytics over the AdhyaapakAI MongoDB. No real database is used:
the service is tested against an in-memory source, and `MongoSource` against a recording
stand-in for the pymongo database."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

from app.api.v1 import app_users as route
from app.main import create_app
from app.services.app_users import (
    REVIEWER_EMAIL,
    DeletionRow,
    MongoSource,
    PurchaseRow,
    RecentSignup,
    SignupRow,
    mask_phone,
    period,
    to_paise,
)
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings

A = "/api/v1/app-users"
OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
URL = "mongodb+srv://dash-reader:s3cret-Pass-word@cluster0.example.mongodb.net/?appName=x"
# 2026-11-03 15:00 IST
NOW = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)


def ist(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC) - timedelta(hours=5, minutes=30)


@dataclass
class FakeSource:
    signup_rows: list[SignupRow] = field(default_factory=list)
    purged: list[datetime] = field(default_factory=list)
    purchase_rows: list[PurchaseRow] = field(default_factory=list)
    recent: list[RecentSignup] = field(default_factory=list)
    deletion_rows: list[DeletionRow] = field(default_factory=list)
    active: dict[str, int] = field(default_factory=dict)
    live: dict[str, set[str]] = field(default_factory=dict)
    fail: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    def signups(self, start: datetime, end: datetime) -> list[SignupRow]:
        self._hit("signups")
        return [r for r in self.signup_rows if start <= r.created < end]

    def purged_signups(self, start: datetime, end: datetime) -> list[datetime]:
        self._hit("purged")
        return [m for m in self.purged if start <= m < end]

    def purchases(self, start: datetime, end: datetime) -> list[PurchaseRow]:
        self._hit("purchases")
        return [p for p in self.purchase_rows if start <= p.created < end]

    def active_since(self, since: datetime) -> int:
        self._hit("active")
        return self.active.get(since.isoformat(), 0)

    def scheduled_deletions(self) -> int:
        self._hit("scheduled")
        return 2

    def total_users(self) -> int:
        self._hit("total")
        return 1234

    def signup_details(self, start: datetime, end: datetime, limit: int) -> list[RecentSignup]:
        self._hit("recent")
        rows = sorted(self.recent, key=lambda r: r.created, reverse=True)
        return [r for r in rows if start <= r.created < end][:limit]

    def deletions(self, start: datetime, end: datetime, limit: int) -> list[DeletionRow]:
        self._hit("deletions")
        return [r for r in self.deletion_rows if r.requested_at and start <= r.requested_at < end][
            :limit
        ]

    def live_user_ids(self, now: datetime) -> dict[str, set[str]]:
        self._hit("live")
        return {k: set(v) for k, v in self.live.items()}

    def close(self) -> None:
        pass


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def uapp(db_path: Path, source: FakeSource, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(route, "utcnow", lambda: NOW)
    application = create_app(
        make_settings(
            db_path, adhyaapak_mongo_url=SecretStr(URL), adhyaapak_mongo_db="doubt_solver_app"
        ),
        start_scheduler=False,
    )
    application.state.app_users_source.close()
    application.state.app_users_source = source
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(uapp: FastAPI) -> Iterator[TestClient]:
    with TestClient(uapp) as c:
        yield c


def error(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


# --- Configuration ---------------------------------------------------------------------


def test_status_reports_configured_without_the_url(api: TestClient) -> None:
    resp = api.get(f"{A}/status", headers=VIEWER)
    assert resp.json() == {"configured": True, "database": "doubt_solver_app"}
    assert "mongodb" not in resp.text and "s3cret" not in resp.text


def test_not_configured(db_path: Path) -> None:
    application = create_app(make_settings(db_path), start_scheduler=False)
    with TestClient(application) as c:
        assert c.get(f"{A}/status", headers=VIEWER).json() == {
            "configured": False,
            "database": None,
        }
        resp = c.get(f"{A}/summary", headers=VIEWER)
        assert resp.status_code == 503 and error(resp) == "app_users_not_configured"
        assert c.get(f"{A}/signups", headers=VIEWER).status_code == 503
    application.state.engine.dispose()


def test_settings_need_both_values_and_a_mongodb_url(db_path: Path) -> None:
    with pytest.raises(ValidationError, match="ADHYAAPAK_MONGO_DB"):
        make_settings(db_path, adhyaapak_mongo_url=SecretStr(URL))
    with pytest.raises(ValidationError, match="mongodb"):
        make_settings(
            db_path, adhyaapak_mongo_url=SecretStr("https://x.test"), adhyaapak_mongo_db="db"
        )
    blank = make_settings(db_path, adhyaapak_mongo_url="", adhyaapak_mongo_db="")
    assert blank.adhyaapak_mongo_url is None and blank.adhyaapak_mongo_db is None


def test_the_url_and_its_password_are_registered_for_log_redaction(db_path: Path) -> None:
    values = make_settings(
        db_path, adhyaapak_mongo_url=SecretStr(URL), adhyaapak_mongo_db="db"
    ).secret_values()
    assert URL in values and "s3cret-Pass-word" in values


def test_requires_login(api: TestClient) -> None:
    assert api.get(f"{A}/summary").status_code == 401


# --- Summary -------------------------------------------------------------------------------


def test_days_are_ist_calendar_days(api: TestClient, source: FakeSource) -> None:
    source.signup_rows = [
        SignupRow(ist(2026, 11, 2, 23, 59), class_level=12, stream="Commerce", goals=("Board",)),
        SignupRow(ist(2026, 11, 3, 0, 1), class_level=11, stream="Science", goals=("JEE", "Board")),
        SignupRow(ist(2026, 11, 3, 14, 0), has_course=True),
    ]
    body = api.get(f"{A}/summary?days=7", headers=VIEWER).json()
    daily = {row["date"]: row["signups"] for row in body["daily"]}
    assert len(body["daily"]) == 7 and body["daily"][0]["date"] == "2026-10-28"
    assert daily["2026-11-02"] == 1 and daily["2026-11-03"] == 2
    assert daily["2026-10-30"] == 0  # empty days are filled with zero
    assert body["timezone"] == "Asia/Kolkata"
    assert body["today"]["date"] == "2026-11-03"
    assert (body["today"]["signups"], body["today"]["signups_yesterday"]) == (2, 1)
    t = body["totals"]
    assert (t["signups"], t["with_course"], t["best_day"], t["best_day_signups"]) == (
        3,
        1,
        "2026-11-03",
        2,
    )
    assert t["average_per_day"] == 0.4 and t["total_users"] == 1234
    assert body["by_class"] == [
        {"label": "Class 11", "count": 1},
        {"label": "Class 12", "count": 1},
        {"label": "Not set", "count": 1},
    ]
    assert body["by_goal"][0] == {"label": "Board", "count": 2}


def test_deleted_accounts_still_count_on_their_signup_day(
    api: TestClient, source: FakeSource
) -> None:
    source.signup_rows = [SignupRow(ist(2026, 10, 30, 10))]
    source.purged = [ist(2026, 10, 30, 11), ist(2026, 9, 1, 11)]  # second one is out of range
    body = api.get(f"{A}/summary?days=7", headers=VIEWER).json()
    row = next(r for r in body["daily"] if r["date"] == "2026-10-30")
    assert (row["signups"], row["deleted_since"]) == (1, 1)
    assert (body["totals"]["signups"], body["totals"]["deleted_since"]) == (2, 1)


def test_purchases_revenue_in_paise_inr_only(api: TestClient, source: FakeSource) -> None:
    source.purchase_rows = [
        PurchaseRow(ist(2026, 11, 3, 10), "razorpay", 1999.0, None),
        PurchaseRow(ist(2026, 11, 3, 11), "app_store", 24.99, "USD"),
        PurchaseRow(ist(2026, 11, 3, 12), "voucher", 0, "INR"),
        PurchaseRow(ist(2026, 11, 1, 12), "app_store", 499, "INR"),
    ]
    body = api.get(f"{A}/summary?days=7", headers=VIEWER).json()
    today = body["today"]
    assert (today["purchases"], today["vouchers"], today["revenue_paise"]) == (2, 1, 199_900)
    t = body["totals"]
    assert (t["purchases"], t["revenue_paise"], t["other_currency_purchases"]) == (3, 249_800, 1)


def test_active_users_and_scheduled_deletions(api: TestClient, source: FakeSource) -> None:
    today_start = ist(2026, 11, 3, 0)
    source.active = {
        today_start.isoformat(): 40,
        (NOW - timedelta(days=7)).isoformat(): 90,
        (NOW - timedelta(days=30)).isoformat(): 150,
    }
    body = api.get(f"{A}/summary", headers=VIEWER).json()
    t = body["today"]
    assert (t["active_today"], t["active_7d"], t["active_30d"]) == (40, 90, 150)
    assert t["deletions_scheduled"] == 2 and body["days"] == 30
    assert body["active_tracked_since"] == "2026-10-28"


def test_results_are_cached_for_a_minute(api: TestClient, source: FakeSource) -> None:
    api.get(f"{A}/summary", headers=VIEWER)
    first = len(source.calls)
    api.get(f"{A}/summary", headers=OWNER)
    assert len(source.calls) == first
    api.get(f"{A}/summary?days=7", headers=VIEWER)  # a different range is its own entry
    assert len(source.calls) > first


@pytest.mark.parametrize("days", [6, 91])
def test_days_range_is_limited(api: TestClient, days: int) -> None:
    assert api.get(f"{A}/summary?days={days}", headers=VIEWER).status_code == 422


# --- Today's list --------------------------------------------------------------------------


ASHA = RecentSignup(
    ist(2026, 11, 3, 14, 5),
    name="Asha Verma",
    email="asha.verma@gmail.com",
    phone="9876543210",
    class_level=12,
    stream="Commerce",
    goals=("Board",),
)


def test_viewers_get_a_masked_list(api: TestClient, source: FakeSource) -> None:
    source.recent = [ASHA, RecentSignup(ist(2026, 11, 2, 23, 0), name="Yesterday Person")]
    resp = api.get(f"{A}/signups", headers=VIEWER)
    body = resp.json()
    assert body["start"] == body["end"] == "2026-11-03"
    assert body["more"] is False and body["masked"] is True
    assert len(body["signups"]) == 1
    row = body["signups"][0]
    assert (row["name"], row["email"], row["phone"], row["class_level"]) == (
        "Asha",
        "a***@gmail.com",
        "******3210",
        12,
    )
    for raw in ("asha.verma", "9876543210", "Verma"):
        assert raw not in resp.text


def test_the_owner_sees_full_contact_details(api: TestClient, source: FakeSource) -> None:
    source.recent = [ASHA]
    body = api.get(f"{A}/signups", headers=OWNER).json()
    row = body["signups"][0]
    assert body["masked"] is False
    assert (row["name"], row["email"], row["phone"]) == (
        "Asha Verma",
        "asha.verma@gmail.com",
        "9876543210",
    )


def test_signups_for_a_chosen_date(api: TestClient, source: FakeSource) -> None:
    source.recent = [
        ASHA,
        RecentSignup(ist(2026, 11, 2, 23, 59), name="Late Night"),
        RecentSignup(ist(2026, 11, 2, 0, 1), name="Early Bird"),
        RecentSignup(ist(2026, 11, 1, 23, 59), name="Day Before"),
    ]
    body = api.get(f"{A}/signups?date=2026-11-02", headers=OWNER).json()
    assert (body["start"], body["end"]) == ("2026-11-02", "2026-11-02")
    assert [r["name"] for r in body["signups"]] == ["Late Night", "Early Bird"]  # newest first
    resp = api.get(f"{A}/signups?date=2026-11-04", headers=OWNER)
    assert resp.status_code == 422 and error(resp) == "range_in_future"
    assert api.get(f"{A}/signups?date=not-a-date", headers=OWNER).status_code == 422


def test_signups_for_a_date_range(api: TestClient, source: FakeSource) -> None:
    source.recent = [
        ASHA,
        RecentSignup(ist(2026, 11, 2, 23, 59), name="Late Night"),
        RecentSignup(ist(2026, 10, 30, 0, 1), name="Early Bird"),
        RecentSignup(ist(2026, 10, 29, 23, 59), name="Too Early"),
    ]
    body = api.get(f"{A}/signups?start=2026-10-30&end=2026-11-02", headers=OWNER).json()
    assert (body["start"], body["end"]) == ("2026-10-30", "2026-11-02")
    assert [r["name"] for r in body["signups"]] == ["Late Night", "Early Bird"]
    for query, code in [
        ("start=2026-10-30", "range_incomplete"),
        ("start=2026-11-02&end=2026-10-30", "range_reversed"),
        ("start=2026-07-01&end=2026-11-01", "range_too_long"),
    ]:
        resp = api.get(f"{A}/signups?{query}", headers=OWNER)
        assert resp.status_code == 422 and error(resp) == code


def test_list_is_capped(api: TestClient, source: FakeSource) -> None:
    source.recent = [RecentSignup(ist(2026, 11, 3, 1) + timedelta(seconds=i)) for i in range(2005)]
    body = api.get(f"{A}/signups", headers=VIEWER).json()
    assert len(body["signups"]) == 2000 and body["more"] is True


# --- Live -----------------------------------------------------------------------------------


def test_online_now_counts_each_person_once_across_signals(
    api: TestClient, source: FakeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    source.live = {
        "app": {"u1", "u2", "u3"},
        "video": {"u2", "u9"},  # u9 watches a video but made no other request in the window
        "tutor": {"u3"},
        "doubts": set(),
        "tests": {"u1"},
    }
    body = api.get(f"{A}/live", headers=VIEWER).json()
    assert body["online_now"] == 4
    assert (body["using_app"], body["watching_video"], body["in_live_tutor"]) == (3, 2, 1)
    assert (body["asking_doubts"], body["taking_test"], body["online_window_minutes"]) == (0, 1, 12)
    assert body["app_open"] is None  # no ADHYAAPAK_JWT_SECRET in this app
    calls = len(source.calls)
    api.get(f"{A}/live", headers=VIEWER)  # same 3-second bucket: served from the cache
    assert len(source.calls) == calls
    monkeypatch.setattr(route, "utcnow", lambda: NOW + timedelta(seconds=20))
    source.live = {"app": {"u1"}}  # the others stopped using the app
    assert api.get(f"{A}/live", headers=VIEWER).json()["online_now"] == 1


# --- Deletions -----------------------------------------------------------------------------


def deletion_rows() -> list[DeletionRow]:
    return [
        DeletionRow(
            "u1",
            "scheduled",
            ist(2026, 11, 2, 10),
            due_at=ist(2026, 11, 9, 10),
            signed_up_at=ist(2026, 10, 1, 9),
            name="Asha Verma",
            email="asha.verma@gmail.com",
            phone="9876543210",
            profile_exists=True,
        ),
        DeletionRow("u2", "completed", ist(2026, 11, 1, 8), completed_at=ist(2026, 11, 1, 8, 1)),
        DeletionRow("u3", "reactivated", ist(2026, 10, 20, 8), profile_exists=True, name="Old"),
    ]


def test_deletions_in_a_range_newest_first(api: TestClient, source: FakeSource) -> None:
    source.deletion_rows = deletion_rows()
    body = api.get(f"{A}/deletions?start=2026-10-30&end=2026-11-03", headers=OWNER).json()
    assert (body["start"], body["end"], body["masked"]) == ("2026-10-30", "2026-11-03", False)
    rows = body["deletions"]
    assert [r["status"] for r in rows] == ["scheduled", "completed"]
    assert (rows[0]["name"], rows[0]["email"], rows[0]["phone"]) == (
        "Asha Verma",
        "asha.verma@gmail.com",
        "9876543210",
    )
    assert rows[0]["due_at"].startswith("2026-11-09")
    # A completed deletion has no profile left, and none is rebuilt.
    assert rows[1]["profile_exists"] is False
    assert (rows[1]["name"], rows[1]["email"], rows[1]["phone"]) == (None, None, None)


def test_deletions_are_masked_for_viewers(api: TestClient, source: FakeSource) -> None:
    source.deletion_rows = deletion_rows()
    resp = api.get(f"{A}/deletions?date=2026-11-02", headers=VIEWER)
    body = resp.json()
    assert body["masked"] is True and len(body["deletions"]) == 1
    row = body["deletions"][0]
    assert (row["name"], row["email"], row["phone"]) == ("Asha", "a***@gmail.com", "******3210")
    assert "asha.verma" not in resp.text and "9876543210" not in resp.text


def test_deletions_default_to_today_and_validate_ranges(
    api: TestClient, source: FakeSource
) -> None:
    source.deletion_rows = deletion_rows()
    body = api.get(f"{A}/deletions", headers=OWNER).json()
    assert body["start"] == body["end"] == "2026-11-03" and body["deletions"] == []
    resp = api.get(f"{A}/deletions?start=2026-11-02&end=2026-11-01", headers=OWNER)
    assert resp.status_code == 422 and error(resp) == "range_reversed"


# --- Custom date range ---------------------------------------------------------------------


def test_custom_range_keeps_the_today_block_on_today(api: TestClient, source: FakeSource) -> None:
    source.signup_rows = [
        SignupRow(ist(2026, 10, 10, 9), class_level=12),
        SignupRow(ist(2026, 10, 11, 9), class_level=11),
        SignupRow(ist(2026, 10, 12, 9)),  # outside the range
        SignupRow(ist(2026, 11, 3, 9)),  # today
        SignupRow(ist(2026, 11, 2, 9)),  # yesterday
    ]
    body = api.get(f"{A}/summary?start=2026-10-10&end=2026-10-11", headers=VIEWER).json()
    assert (body["start"], body["end"], body["days"]) == ("2026-10-10", "2026-10-11", 2)
    assert [r["date"] for r in body["daily"]] == ["2026-10-10", "2026-10-11"]
    assert body["totals"]["signups"] == 2
    assert body["by_class"] == [
        {"label": "Class 11", "count": 1},
        {"label": "Class 12", "count": 1},
    ]
    assert (body["today"]["signups"], body["today"]["signups_yesterday"]) == (1, 1)


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("start=2026-10-10", "range_incomplete"),
        ("start=2026-10-12&end=2026-10-10", "range_reversed"),
        ("start=2026-11-01&end=2026-11-04", "range_in_future"),
        ("start=2026-07-01&end=2026-11-01", "range_too_long"),
    ],
)
def test_bad_ranges_are_refused(api: TestClient, query: str, code: str) -> None:
    resp = api.get(f"{A}/summary?{query}", headers=VIEWER)
    assert resp.status_code == 422 and error(resp) == code


# --- Errors and logging --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (
            ServerSelectionTimeoutError("cluster0.example.mongodb.net:27017"),
            504,
            "app_users_timeout",
        ),
        (OperationFailure("bad auth", code=18), 502, "app_users_forbidden"),
        (OperationFailure("weird", code=2), 502, "app_users_query_failed"),
    ],
)
def test_database_errors_are_mapped(
    api: TestClient, source: FakeSource, exc: Exception, status: int, code: str
) -> None:
    source.fail = exc
    resp = api.get(f"{A}/summary", headers=VIEWER)
    assert resp.status_code == status and error(resp) == code
    assert "mongodb.net" not in resp.text


def test_logs_carry_no_personal_data_or_url(
    api: TestClient, source: FakeSource, caplog: pytest.LogCaptureFixture
) -> None:
    source.recent = [
        RecentSignup(ist(2026, 11, 3, 9), name="Asha", email="asha@x.com", phone="9876543210")
    ]
    caplog.set_level(logging.DEBUG)
    api.get(f"{A}/signups", headers=OWNER)
    source.fail = ServerSelectionTimeoutError("dash-reader@cluster0.example.mongodb.net")
    api.get(f"{A}/summary", headers=VIEWER)
    text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for raw in ("asha@x.com", "9876543210", "mongodb.net", "s3cret"):
        assert raw not in text


# --- Helpers ---------------------------------------------------------------------------------


def test_helpers() -> None:
    assert mask_phone("+91 98765 43210") == "******3210"
    assert mask_phone("123") == "***" and mask_phone(None) is None
    assert to_paise(1999.995) == 200_000 and to_paise("x") == 0 and to_paise(-5) == 0
    from zoneinfo import ZoneInfo

    days = period(3, ZoneInfo("Asia/Kolkata"), datetime(2026, 11, 2, 19, 0, tzinfo=UTC))
    assert days == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]  # 00:30 IST


# --- MongoSource against a recording stand-in ------------------------------------------------


class RecordingCursor(list[dict[str, Any]]):
    def sort(self, spec: Any) -> RecordingCursor:
        self.sorted_by = spec
        return self


class RecordingCollection:
    def __init__(self, log: list[tuple[str, str, Any]], name: str, docs: list[dict[str, Any]]):
        self.log, self.name, self.docs = log, name, docs

    def find(self, flt: dict[str, Any], projection: dict[str, int], **kw: Any) -> RecordingCursor:
        self.log.append((self.name, "find", {"filter": flt, "projection": projection, **kw}))
        return RecordingCursor(self.docs)

    def count_documents(self, flt: dict[str, Any], **kw: Any) -> int:
        self.log.append((self.name, "count_documents", {"filter": flt, **kw}))
        return 7

    def __getattr__(self, attr: str) -> Any:  # any other call (a write) fails the test
        raise AssertionError(f"unexpected call {attr} on {self.name}")


class RecordingClient:
    def __init__(self, docs: dict[str, list[dict[str, Any]]]) -> None:
        self.log: list[tuple[str, str, Any]] = []
        self.docs = docs

    def __getitem__(self, db: str) -> RecordingDb:
        assert db == "doubt_solver_app"
        return RecordingDb(self)

    def close(self) -> None:
        pass


class RecordingDb:
    def __init__(self, client: RecordingClient) -> None:
        self.client = client

    def __getitem__(self, name: str) -> RecordingCollection:
        return RecordingCollection(self.client.log, name, self.client.docs.get(name, []))


def test_mongo_source_only_reads_with_time_limits() -> None:
    created = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
    oid = ObjectId.from_datetime(created)
    client = RecordingClient(
        {
            "users": [{"_id": oid, "class": 12, "goals": ["Board", 5], "entitlements": ["x"]}],
            "account_deletions": [
                {"_id": str(oid), "status": "scheduled", "requested_at": created}
            ],
            "entitlement_purchases": [
                # naive UTC, as the app stores it
                {
                    "created_at": datetime(2026, 11, 3, 4),  # noqa: DTZ001
                    "store": "razorpay",
                    "amount_inr": 10,
                }
            ],
        }
    )
    src = MongoSource("mongodb://unused", "doubt_solver_app", client=client)
    start, end = created - timedelta(days=1), created + timedelta(days=1)
    rows = src.signups(start, end)
    assert rows[0].created == created and rows[0].goals == ("Board",) and rows[0].has_course
    assert src.purged_signups(start, end) == [created]
    assert src.purchases(start, end)[0].created.tzinfo is not None
    assert src.active_since(start) == 7 and src.total_users() == 7
    src.signup_details(start, end, 2001)
    live = src.live_user_ids(created)
    assert live["app"] == {str(oid)} and live["tutor"] == set()
    rows = src.deletions(start, end, 2001)
    assert [r.user_id for r in rows] == [str(oid)] and rows[0].profile_exists
    assert rows[0].requested_at == created and rows[0].signed_up_at == created

    for coll, op, args in client.log:
        assert op in ("find", "count_documents")
        assert coll in MongoSource.COLLECTIONS
        assert args.get("max_time_ms") == 10_000 or args.get("maxTimeMS") == 10_000
        if coll == "users" and "$in" not in args["filter"].get("_id", {}):  # not the by-id lookup
            assert args["filter"]["email"] == {"$ne": REVIEWER_EMAIL}
    users_find = next(a for c, o, a in client.log if c == "users" and o == "find")
    assert set(users_find["filter"]["_id"]) == {"$gte", "$lt"}  # indexed _id range
    assert "email" not in users_find["projection"]  # the chart query reads no contact data
    with pytest.raises(ValueError, match="not readable"):
        src._coll("wallets")
