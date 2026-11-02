"""Voucher management: Course and Live Tutor voucher CRUD, subject/package targeting,
discounts, activate/deactivate, lock/unlock, the configurable user limit (default 1,000) under
concurrency, validation and redemption rules, summaries and analytics from real usage events,
redemption history, audit trail and permissions."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, insert, select

from app.core.errors import ConflictError
from app.core.time import utcnow
from app.main import create_app
from app.models import AuditLog, Voucher, VoucherUsageEvent, VoucherUser
from app.models.enums import (
    Actor,
    VoucherActor,
    VoucherCategory,
    VoucherDiscountType,
    VoucherEventOutcome,
    VoucherEventSource,
)
from app.schemas.voucher import RedeemRequest, VoucherUserCreate
from app.services.context import RequestContext
from app.services.vouchers import VoucherContext, VoucherService, discount_for
from tests.conftest import OWNER_TOKEN, VIEWER_TOKEN, make_settings

API_TOKEN = "voucher-api-token-0123456789-abcdefghijklmnop"
V = "/api/v1/vouchers"
C = f"{V}/courses"
L = f"{V}/live-tutor"
I = "/api/v1/integrations/vouchers"  # noqa: E741
OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
APP = {"Authorization": f"Bearer {API_TOKEN}"}
OWNER_CTX = VoucherContext(VoucherActor.OWNER, RequestContext(actor=Actor.OWNER))
EDITABLE = (
    "code",
    "name",
    "description",
    "discount_type",
    "discount_value",
    "currency",
    "applies_to_all",
    "min_purchase_amount",
    "benefit_validity_days",
    "max_uses",
    "max_uses_per_user",
    "max_unique_users",
    "valid_from",
    "valid_until",
    "is_active",
)
_orders = itertools.count(1)


@pytest.fixture
def vapp(db_path: Path) -> Iterator[FastAPI]:
    application = create_app(
        make_settings(db_path, voucher_api_token=SecretStr(API_TOKEN)), start_scheduler=False
    )
    yield application
    application.state.engine.dispose()


@pytest.fixture
def api(vapp: FastAPI) -> Iterator[TestClient]:
    with TestClient(vapp) as c:
        yield c


def body_for(base: str, **fields) -> dict:  # type: ignore[no-untyped-def]
    body = {"discount_type": "percentage", "discount_value": "10", **fields}
    if not {"subjects", "packages", "applies_to_all"} & fields.keys():
        body["applies_to_all"] = True
    return body


def create(api: TestClient, base: str = C, **fields) -> dict:  # type: ignore[no-untyped-def]
    resp = api.post(base, json=body_for(base, **fields), headers=OWNER)
    assert resp.status_code == 201, resp.text
    return resp.json()


def editable(v: dict) -> dict:
    body = {k: v[k] for k in EDITABLE}
    if v["category"] == "course":
        body["subjects"] = v["subjects"]
    else:
        body["packages"] = v["packages"]
    return {**body, "version": v["version"]}


def base_of(v: dict) -> str:
    return C if v["category"] == "course" else L


def user(api: TestClient, external_id: str, headers: dict = OWNER) -> dict:
    path = f"{I}/users" if headers is APP else f"{V}/users"
    resp = api.post(path, json={"external_id": external_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def order(code: str, ext: str, **extra) -> dict:  # type: ignore[no-untyped-def]
    return {
        "code": code,
        "user_external_id": ext,
        "category": "course",
        "item": "Mathematics",
        "order_amount": "1000.00",
        "currency": "INR",
        "order_reference": f"ORD-{next(_orders)}",
        **extra,
    }


def redeem(api: TestClient, code: str, ext: str, headers: dict = OWNER, **extra):  # type: ignore[no-untyped-def]
    path = f"{I}/redeem" if headers is APP else f"{V}/redeem"
    return api.post(path, json=order(code, ext, **extra), headers=headers)


def validate(api: TestClient, code: str, ext: str, /, **extra) -> dict:  # type: ignore[no-untyped-def]
    body = {**order(code, ext), **extra}
    body.pop("order_reference")
    resp = api.post(f"{V}/validate", json=body, headers=OWNER)
    assert resp.status_code == 200, resp.text
    return resp.json()


def error(resp) -> str:  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def audit_actions(vapp: FastAPI) -> list[str]:
    with vapp.state.session_factory() as s:
        return list(s.scalars(select(AuditLog.action).order_by(AuditLog.id)))


# --- CRUD ------------------------------------------------------------------------------------


def test_course_voucher_lifecycle_with_system_managed_properties(api: TestClient) -> None:
    v = create(
        api,
        code="welcome-10",
        subjects=["Mathematics"],
        max_uses=100,
        max_unique_users=50,
        min_purchase_amount="499.00",
        benefit_validity_days=30,
        description="First order",
    )
    assert v["code"] == "WELCOME-10" and v["name"] == "WELCOME-10"  # label defaults to code
    assert (v["category"], v["subjects"], v["packages"], v["applies_to_all"]) == (
        "course",
        ["Mathematics"],
        [],
        False,
    )
    assert (v["discount_type"], v["discount_value"], v["currency"]) == ("percentage", "10", "INR")
    assert (v["min_purchase_amount"], v["benefit_validity_days"]) == ("499", 30)
    assert (v["status"], v["use_count"], v["remaining_uses"], v["version"]) == ("active", 0, 100, 1)
    assert (v["unique_user_count"], v["remaining_unique_users"]) == (0, 50)
    assert (v["created_by"], v["updated_by"], v["is_locked"], v["can_delete"]) == (
        "owner",
        "owner",
        False,
        True,
    )
    generated = create(api)
    assert generated["code"].startswith("VCH-") and len(generated["code"]) == 12

    # Read: list with search, filter, sort and pagination; get by id
    page = api.get(C, params={"q": "welcome", "limit": 1}, headers=VIEWER).json()
    assert page["total"] == 1 and page["items"][0]["id"] == v["id"]
    assert api.get(C, params={"status": "active"}, headers=VIEWER).json()["total"] == 2
    assert api.get(f"{C}/{v['id']}", headers=VIEWER).json()["code"] == "WELCOME-10"
    # Categories are separate: a course voucher is not visible under Live Tutor
    assert api.get(L, headers=VIEWER).json()["total"] == 0
    assert api.get(f"{L}/{v['id']}", headers=VIEWER).status_code == 404

    # Update: needs the current version; metadata moves on automatically
    resp = api.put(
        f"{C}/{v['id']}",
        json={**editable(v), "name": "Welcome 10%", "subjects": ["Mathematics", "Physics"]},
        headers=OWNER,
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert (updated["name"], updated["version"], updated["subjects"]) == (
        "Welcome 10%",
        2,
        ["Mathematics", "Physics"],
    )
    assert updated["updated_at"] >= v["updated_at"]
    stale = api.put(f"{C}/{v['id']}", json=editable(v), headers=OWNER)
    assert stale.status_code == 409 and error(stale) == "concurrent_modification"

    # System-managed fields are never accepted from a request
    sneaky = api.post(C, json={**body_for(C), "use_count": 99, "is_locked": True}, headers=OWNER)
    assert sneaky.status_code == 422

    # Delete (unused, unlocked)
    assert api.delete(f"{C}/{generated['id']}", headers=OWNER).status_code == 204
    assert api.get(f"{C}/{generated['id']}", headers=OWNER).status_code == 404


def test_course_subject_targeting(api: TestClient) -> None:
    one = create(api, code="ONESUB", subjects=["  Mathematics "])
    many = create(api, code="MANYSUB", subjects=["Physics", "chemistry", "PHYSICS", "Biology"])
    every = create(api, code="ALLSUB", applies_to_all=True)
    assert one["subjects"] == ["Mathematics"]  # trimmed
    assert many["subjects"] == ["Physics", "chemistry", "Biology"]  # de-duplicated, any case
    assert (every["applies_to_all"], every["subjects"]) == (True, [])

    # Editing restores and changes the selection, including only a change of letter case
    edited = api.put(
        f"{C}/{many['id']}",
        json={**editable(many), "subjects": ["Chemistry", "Biology", "English"]},
        headers=OWNER,
    ).json()
    assert edited["subjects"] == ["Chemistry", "Biology", "English"]
    assert api.get(f"{C}/{many['id']}", headers=OWNER).json()["subjects"] == edited["subjects"]

    # Filter by subject: vouchers listed for it, plus those that apply to all subjects
    codes = lambda params: sorted(  # noqa: E731
        i["code"] for i in api.get(C, params=params, headers=VIEWER).json()["items"]
    )
    assert codes({"applies_to": "biology"}) == ["ALLSUB", "MANYSUB"]
    assert codes({"applies_to": "Mathematics"}) == ["ALLSUB", "ONESUB"]
    assert codes({"q": "engl"}) == ["MANYSUB"]  # search covers subject names
    # Subject suggestions for the form come from existing course vouchers
    assert api.get(f"{V}/subjects", headers=VIEWER).json()["items"] == [
        "Biology",
        "Chemistry",
        "English",
        "Mathematics",
    ]

    for bad, field in (
        ({"subjects": []}, "subjects"),  # nothing selected and not "all"
        ({"applies_to_all": True, "subjects": ["Maths"]}, "subjects"),
        ({"packages": ["pro"]}, "packages"),  # packages belong to Live Tutor vouchers
        ({"subjects": ["x" * 81]}, "subjects"),
    ):
        resp = api.post(C, json=body_for(C, **bad), headers=OWNER)
        assert resp.status_code == 422, (bad, resp.text)
        assert field in str(resp.json()["error"]["details"])


def test_live_tutor_package_targeting(api: TestClient) -> None:
    created = {
        name: create(api, L, code=f"LT-{name.upper()}", packages=pkgs)
        for name, pkgs in (
            ("beginner", ["beginner"]),
            ("intermediate", ["intermediate"]),
            ("pro", ["pro"]),
            ("combo", ["pro", "beginner", "pro"]),
        )
    }
    every = create(api, L, code="LT-ALL", applies_to_all=True)
    assert created["beginner"]["packages"] == ["beginner"]
    assert created["intermediate"]["packages"] == ["intermediate"]
    assert created["pro"]["packages"] == ["pro"]
    assert created["combo"]["packages"] == ["beginner", "pro"]  # canonical order, no repeats
    assert (every["applies_to_all"], every["packages"], every["category"]) == (
        True,
        [],
        "live_tutor",
    )
    combo = created["combo"]
    edited = api.put(
        f"{L}/{combo['id']}",
        json={**editable(combo), "packages": ["intermediate", "pro"]},
        headers=OWNER,
    ).json()
    assert edited["packages"] == ["intermediate", "pro"]
    pro = api.get(L, params={"applies_to": "pro"}, headers=VIEWER).json()["items"]
    assert sorted(i["code"] for i in pro) == ["LT-ALL", "LT-COMBO", "LT-PRO"]
    for bad in ({"packages": ["platinum"]}, {"subjects": ["Maths"]}, {"packages": []}):
        assert api.post(L, json=body_for(L, **bad), headers=OWNER).status_code == 422
    assert api.get(C, headers=VIEWER).json()["total"] == 0


def test_validation_and_duplicates(api: TestClient) -> None:
    create(api, code="SUMMER25")
    dup = api.post(C, json={**body_for(C), "code": "summer25"}, headers=OWNER)
    assert dup.status_code == 409 and error(dup) == "duplicate_voucher"
    # Codes are unique across both categories
    dup = api.post(L, json={**body_for(L), "code": "SUMMER25"}, headers=OWNER)
    assert dup.status_code == 409
    for bad in (
        {"code": "a"},  # too short
        {"code": "bad code!"},
        {"discount_value": "0"},
        {"discount_value": "100.5"},  # percentage above 100
        {"discount_value": "-5", "discount_type": "fixed"},
        {"discount_value": 10.5},  # floats are refused for money
        {"discount_value": "10.123"},  # too many decimal places
        {"discount_type": "bogus"},
        {"currency": "JPY"},  # not a supported currency
        {"min_purchase_amount": "-1"},
        {"benefit_validity_days": 0},
        {"valid_from": "2026-10-10T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z"},
        {"max_uses": 5, "max_uses_per_user": 6},
        {"max_uses": 5, "max_unique_users": 6},
        {"max_uses": 0},
        {"valid_from": "2026-10-10T00:00:00"},  # no timezone
    ):
        resp = api.post(C, json=body_for(C, **bad), headers=OWNER)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["details"]  # field-level messages for the form
    fixed = create(api, code="FLAT200", discount_type="fixed", discount_value="200")
    assert (fixed["discount_type"], fixed["discount_value"]) == ("fixed", "200")


def test_activate_and_deactivate(api: TestClient, vapp: FastAPI) -> None:
    v = create(api, code="TOGGLE")
    user(api, "t1")
    off = api.post(f"{C}/{v['id']}/deactivate", headers=OWNER)
    assert off.status_code == 200 and (off.json()["is_active"], off.json()["status"]) == (
        False,
        "inactive",
    )
    again = api.post(f"{C}/{v['id']}/deactivate", headers=OWNER)
    assert again.status_code == 409 and error(again) == "already_inactive"
    refused = redeem(api, "TOGGLE", "t1")
    assert refused.status_code == 409 and error(refused) == "voucher_inactive"
    assert api.post(f"{C}/{v['id']}/activate", headers=VIEWER).status_code == 403
    assert api.post(f"{L}/{v['id']}/activate", headers=OWNER).status_code == 404
    on = api.post(f"{C}/{v['id']}/activate", headers=OWNER).json()
    assert (on["is_active"], on["status"]) == (True, "active")
    assert redeem(api, "TOGGLE", "t1").status_code == 200
    actions = audit_actions(vapp)
    assert "voucher.deactivate" in actions and "voucher.activate" in actions


def test_listing_filters_and_sorting(api: TestClient) -> None:
    now = datetime.now(UTC)
    create(api, code="PCT5", discount_value="5", max_uses=10)
    create(api, code="PCT50", discount_value="50", max_uses=30)
    create(api, code="FIXED", discount_type="fixed", discount_value="100", max_uses=20)
    create(
        api,
        code="OLD",
        valid_until=(now - timedelta(days=1)).isoformat(),
        valid_from=(now - timedelta(days=10)).isoformat(),
    )
    create(api, code="SOON", valid_from=(now + timedelta(days=5)).isoformat())

    def codes(**params) -> list[str]:  # type: ignore[no-untyped-def]
        return [i["code"] for i in api.get(C, params=params, headers=VIEWER).json()["items"]]

    assert codes(discount_type="fixed") == ["FIXED"]
    assert codes(status="expired") == ["OLD"]
    assert codes(status="scheduled") == ["SOON"]
    assert codes(sort="max_uses", order="asc")[-3:] == ["PCT5", "FIXED", "PCT50"]
    assert codes(sort="code", order="asc") == ["FIXED", "OLD", "PCT5", "PCT50", "SOON"]
    # Validity period: campaigns running at some point in the next 2 days (not OLD, not SOON)
    window = {"valid_start": now.isoformat(), "valid_end": (now + timedelta(days=2)).isoformat()}
    assert sorted(codes(**window)) == ["FIXED", "PCT5", "PCT50"]
    page = api.get(
        C, params={"limit": 2, "offset": 4, "sort": "code", "order": "asc"}, headers=VIEWER
    ).json()
    assert page["total"] == 5 and [i["code"] for i in page["items"]] == ["SOON"]


def test_used_voucher_keeps_its_code_and_cannot_be_deleted(api: TestClient) -> None:
    v = create(api, code="ONCE", max_uses=3)
    user(api, "student-1")
    assert redeem(api, "ONCE", "student-1").status_code == 200
    current = api.get(f"{C}/{v['id']}", headers=OWNER).json()
    renamed = api.put(f"{C}/{v['id']}", json={**editable(current), "code": "TWICE"}, headers=OWNER)
    assert renamed.status_code == 409 and error(renamed) == "code_in_use"
    too_few_users = api.put(
        f"{C}/{v['id']}", json={**editable(current), "max_unique_users": 1}, headers=OWNER
    )
    assert too_few_users.status_code == 200  # 1 user reached so far: allowed
    current = too_few_users.json()
    lowered = api.put(f"{C}/{v['id']}", json={**editable(current), "max_uses": 1}, headers=OWNER)
    assert lowered.status_code == 200  # 1 == use_count is allowed
    deleted = api.delete(f"{C}/{v['id']}", headers=OWNER)
    assert deleted.status_code == 409 and error(deleted) == "voucher_in_use"
    assert current["can_delete"] is False


# --- lock / unlock ---------------------------------------------------------------------------


def test_lock_blocks_edit_delete_and_use_and_records_who_and_when(
    api: TestClient, vapp: FastAPI
) -> None:
    v = create(api, code="LOCKME")
    user(api, "u1")
    locked = api.post(f"{C}/{v['id']}/lock", json={"reason": "suspected abuse"}, headers=OWNER)
    assert locked.status_code == 200
    body = locked.json()
    assert (body["status"], body["is_locked"], body["lock_reason"], body["locked_by"]) == (
        "locked",
        True,
        "suspected abuse",
        "owner",
    )
    assert body["locked_at"] is not None and body["can_delete"] is False

    resp = api.put(f"{C}/{v['id']}", json=editable(body), headers=OWNER)
    assert resp.status_code == 409 and error(resp) == "voucher_locked"
    resp = api.delete(f"{C}/{v['id']}", headers=OWNER)
    assert resp.status_code == 409 and error(resp) == "voucher_locked"
    resp = api.post(f"{C}/{v['id']}/deactivate", headers=OWNER)
    assert resp.status_code == 409 and error(resp) == "voucher_locked"
    resp = redeem(api, "LOCKME", "u1")
    assert resp.status_code == 409 and error(resp) == "voucher_locked"
    again = api.post(f"{C}/{v['id']}/lock", json={}, headers=OWNER)
    assert again.status_code == 409 and error(again) == "already_locked"

    # Only the owner can lock/unlock, enforced on the server
    assert api.post(f"{C}/{v['id']}/unlock", headers=VIEWER).status_code == 403
    assert api.post(f"{C}/{v['id']}/unlock").status_code == 401

    unlocked = api.post(f"{C}/{v['id']}/unlock", headers=OWNER).json()
    assert (unlocked["is_locked"], unlocked["unlocked_by"], unlocked["lock_reason"]) == (
        False,
        "owner",
        None,
    )
    assert unlocked["unlocked_at"] is not None
    assert redeem(api, "LOCKME", "u1").status_code == 200
    actions = audit_actions(vapp)
    assert actions.count("voucher.lock") == 1 and actions.count("voucher.unlock") == 1
    assert "voucher.redeem_failed" in actions


# --- user limit ------------------------------------------------------------------------------


def test_default_user_limit_is_1000(api: TestClient, vapp: FastAPI) -> None:
    cap = api.get(f"{V}/capacity", headers=VIEWER).json()
    assert cap == {
        "user_limit": 1000,
        "total_users": 0,
        "remaining": 1000,
        "percent_used": 0.0,
        "default_limit": 1000,
    }
    now = utcnow()
    with vapp.state.session_factory() as s, s.begin():
        s.execute(
            insert(VoucherUser),
            [
                {"external_id": f"bulk-{i}", "created_at": now, "created_by": VoucherActor.SYSTEM}
                for i in range(999)
            ],
        )
    user(api, "user-1000")  # the 1,000th fits
    full = api.post(f"{V}/users", json={"external_id": "user-1001"}, headers=OWNER)
    assert full.status_code == 409 and error(full) == "user_limit_reached"
    cap = api.get(f"{V}/capacity", headers=OWNER).json()
    assert (cap["total_users"], cap["remaining"], cap["percent_used"]) == (1000, 0, 100.0)


def test_user_limit_is_configurable_and_never_below_current(api: TestClient, vapp: FastAPI) -> None:
    assert (
        api.put(f"{V}/settings/user-limit", json={"user_limit": 2}, headers=VIEWER).status_code
        == 403
    )
    resp = api.put(f"{V}/settings/user-limit", json={"user_limit": 2}, headers=OWNER)
    assert resp.status_code == 200 and resp.json()["user_limit"] == 2
    user(api, "a")
    user(api, "b", headers=OWNER)
    over = api.post(f"{V}/users", json={"external_id": "c"}, headers=OWNER)
    assert over.status_code == 409 and error(over) == "user_limit_reached"
    below = api.put(f"{V}/settings/user-limit", json={"user_limit": 1}, headers=OWNER)
    assert below.status_code == 409 and error(below) == "limit_below_current"
    assert (
        api.put(f"{V}/settings/user-limit", json={"user_limit": 3}, headers=OWNER).status_code
        == 200
    )
    user(api, "c")
    dup = api.post(f"{V}/users", json={"external_id": "c"}, headers=OWNER)
    assert dup.status_code == 409 and error(dup) == "duplicate_user"
    assert "voucher.user_limit" in audit_actions(vapp)
    # the limit survives a restart (stored in the database, not in memory)
    restarted = create_app(vapp.state.settings, start_scheduler=False)
    with TestClient(restarted) as c:
        assert c.get(f"{V}/capacity", headers=OWNER).json()["user_limit"] == 3
    restarted.state.engine.dispose()


def test_concurrent_registrations_never_exceed_the_limit(api: TestClient, vapp: FastAPI) -> None:
    api.put(f"{V}/settings/user-limit", json={"user_limit": 5}, headers=OWNER)
    settings = vapp.state.settings

    def register(i: int) -> str:
        with vapp.state.session_factory() as s:
            try:
                VoucherService(s, settings).register_user(
                    VoucherUserCreate(external_id=f"racer-{i}"), OWNER_CTX
                )
                return "ok"
            except ConflictError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(register, range(30)))
    assert results.count("ok") == 5
    assert results.count("user_limit_reached") == 25
    with vapp.state.session_factory() as s:
        assert s.scalar(select(func.count()).select_from(VoucherUser)) == 5


# --- discounts, validation and redemption ---------------------------------------------------------


def test_discount_calculation() -> None:
    pct, fixed = VoucherDiscountType.PERCENTAGE, VoucherDiscountType.FIXED
    assert discount_for(pct, Decimal("10"), Decimal("999.99"), "INR") == Decimal("100.00")
    assert discount_for(pct, Decimal("12.5"), Decimal("10.01"), "INR") == Decimal("1.25")
    assert discount_for(pct, Decimal("100"), Decimal("499"), "INR") == Decimal("499")
    assert discount_for(fixed, Decimal("200"), Decimal("999"), "INR") == Decimal("200")
    assert discount_for(fixed, Decimal("200"), Decimal("150"), "INR") == Decimal("150")  # capped


def test_validate_checks_the_order_without_recording_anything(
    api: TestClient, vapp: FastAPI
) -> None:
    now = datetime.now(UTC)
    create(
        api,
        code="MATHS20",
        subjects=["Mathematics"],
        discount_value="20",
        min_purchase_amount="500",
        benefit_validity_days=30,
    )
    create(api, L, code="PRO300", packages=["pro"], discount_type="fixed", discount_value="300")
    user(api, "v1")

    ok = validate(api, "maths20", "v1", order_amount="1234.50")
    assert ok["valid"] is True and ok["reason"] is None
    assert (ok["discount_amount"], ok["final_amount"]) == ("246.9", "987.6")
    expires = datetime.fromisoformat(ok["benefit_expires_at"])
    assert timedelta(days=29, hours=23) < expires - now < timedelta(days=30, minutes=5)

    tutor = validate(api, "PRO300", "v1", category="live_tutor", item="PRO", order_amount="250")
    assert (tutor["valid"], tutor["discount_amount"], tutor["final_amount"]) == (True, "250", "0")
    assert tutor["benefit_expires_at"] is None  # no benefit validity configured

    for extra, reason in (
        ({"item": "Physics"}, "not_applicable"),
        ({"order_amount": "499.99"}, "below_minimum"),
        ({"currency": "USD"}, "currency_mismatch"),
        ({"category": "live_tutor", "item": "beginner"}, "wrong_category"),
        ({"code": "NOSUCH"}, "not_found"),
        ({"user_external_id": "stranger"}, "user_not_registered"),
    ):
        result = validate(api, "MATHS20", "v1", **extra)
        assert (result["valid"], result["reason"], result["discount_amount"]) == (
            False,
            reason,
            "0",
        ), extra
        assert result["message"]
    platinum = order("PRO300", "v1", category="live_tutor", item="platinum")
    platinum.pop("order_reference")
    bad_package = api.post(f"{V}/validate", json=platinum, headers=OWNER)
    assert bad_package.status_code == 422
    with vapp.state.session_factory() as s:
        assert s.scalar(select(func.count()).select_from(VoucherUsageEvent)) == 0
    assert api.post(f"{V}/validate", json={}, headers=VIEWER).status_code == 403


def test_redemption_rules_and_every_failed_attempt_is_recorded(
    api: TestClient, vapp: FastAPI
) -> None:
    now = datetime.now(UTC)
    create(api, code="OK1", max_uses=2, max_uses_per_user=1)
    create(api, code="OFF", is_active=False)
    create(api, code="LATER", valid_from=(now + timedelta(days=1)).isoformat())
    create(
        api,
        code="GONE",
        valid_from=(now - timedelta(days=2)).isoformat(),
        valid_until=(now - timedelta(days=1)).isoformat(),
    )
    create(api, code="SCIENCE", subjects=["Physics"], min_purchase_amount="2000")
    create(api, L, code="BEGIN", packages=["beginner"])
    user(api, "u1")
    user(api, "u2")
    user(api, "u3")

    ok = redeem(api, "ok1", "u1")  # code is case-insensitive
    assert ok.status_code == 200 and (ok.json()["use_count"], ok.json()["remaining_uses"]) == (1, 1)
    assert (ok.json()["discount_amount"], ok.json()["final_amount"]) == ("100", "900")
    cases = [
        (("OK1", "u1", {}), 409, "voucher_per_user_limit"),
        (("NOPE", "u1", {}), 404, "voucher_not_found"),
        (("OK1", "stranger", {}), 404, "voucher_user_not_found"),
        (("OFF", "u1", {}), 409, "voucher_inactive"),
        (("LATER", "u1", {}), 409, "voucher_not_started"),
        (("GONE", "u1", {}), 409, "voucher_expired"),
        (("SCIENCE", "u1", {}), 409, "voucher_not_applicable"),
        (("SCIENCE", "u1", {"item": "physics"}), 409, "voucher_below_minimum"),
        (("BEGIN", "u1", {}), 409, "voucher_wrong_category"),
        (("BEGIN", "u1", {"category": "live_tutor", "item": "pro"}), 409, "voucher_not_applicable"),
    ]
    for (code, ext, extra), status, err in cases:
        resp = redeem(api, code, ext, **extra)
        assert (resp.status_code, error(resp)) == (status, err), (code, ext, resp.text)
    tutor = redeem(api, "BEGIN", "u1", category="live_tutor", item="Beginner")
    assert tutor.status_code == 200 and tutor.json()["item"] == "beginner"
    assert redeem(api, "OK1", "u2").status_code == 200
    exhausted = redeem(api, "OK1", "u3")
    assert exhausted.status_code == 409 and error(exhausted) == "voucher_exhausted"

    with vapp.state.session_factory() as s:
        events = list(s.scalars(select(VoucherUsageEvent).order_by(VoucherUsageEvent.id)))
    assert [e.outcome for e in events].count(VoucherEventOutcome.SUCCESS) == 3
    assert [e.failure_reason for e in events if e.outcome == VoucherEventOutcome.FAILED] == [
        "per_user_limit",
        "not_found",
        "user_not_registered",
        "inactive",
        "not_started",
        "expired",
        "not_applicable",
        "below_minimum",
        "wrong_category",
        "not_applicable",
        "exhausted",
    ]
    assert all(
        e.actor is VoucherActor.OWNER and e.source is VoucherEventSource.DASHBOARD for e in events
    )
    # Failed attempts never consume a use or a unique-user slot
    science = api.get(C, params={"q": "SCIENCE"}, headers=OWNER).json()["items"][0]
    assert (science["use_count"], science["unique_user_count"]) == (0, 0)


def test_one_order_cannot_use_the_same_voucher_twice(api: TestClient) -> None:
    create(api, code="REPEAT")
    user(api, "r1")
    first = redeem(api, "REPEAT", "r1", order_reference="ORDER-77")
    assert first.status_code == 200
    retry = redeem(api, "REPEAT", "r1", order_reference="ORDER-77")
    assert retry.status_code == 409 and error(retry) == "voucher_order_already_redeemed"
    assert redeem(api, "REPEAT", "r1", order_reference="ORDER-78").status_code == 200


def test_benefit_expiry_and_redemption_history(api: TestClient) -> None:
    v = create(
        api,
        L,
        code="TUTOR7",
        packages=["intermediate"],
        benefit_validity_days=7,
        discount_type="fixed",
        discount_value="150",
    )
    user(api, "h1", headers=OWNER)
    ok = redeem(
        api,
        "TUTOR7",
        "h1",
        category="live_tutor",
        item="intermediate",
        order_reference="BOOKING-1",
        order_amount="1200",
    )
    assert ok.status_code == 200, ok.text
    result = ok.json()
    occurred = datetime.fromisoformat(result["occurred_at"])
    assert datetime.fromisoformat(result["benefit_expires_at"]) - occurred == timedelta(days=7)
    redeem(api, "TUTOR7", "nobody", category="live_tutor", item="intermediate")

    history = api.get(f"{V}/usage-events", params={"voucher_id": v["id"]}, headers=VIEWER).json()
    assert history["total"] == 2
    success = next(e for e in history["items"] if e["outcome"] == "success")
    assert (
        success["order_reference"],
        success["item"],
        success["order_amount"],
        success["discount_amount"],
        success["currency"],
        success["category"],
    ) == ("BOOKING-1", "intermediate", "1200", "150", "INR", "live_tutor")
    assert success["benefit_expires_at"] == result["benefit_expires_at"]
    only_ok = api.get(
        f"{V}/usage-events", params={"voucher_id": v["id"], "outcome": "success"}, headers=VIEWER
    ).json()
    assert only_ok["total"] == 1
    by_order = api.get(f"{V}/usage-events", params={"q": "booking-1"}, headers=VIEWER).json()
    assert by_order["total"] == 1
    by_category = api.get(f"{V}/usage-events", params={"category": "course"}, headers=VIEWER)
    assert by_category.json()["total"] == 0
    after = api.get(f"{L}/{v['id']}", headers=VIEWER).json()
    assert (after["use_count"], after["unique_user_count"]) == (1, 1)


def test_concurrent_redemptions_respect_max_uses(api: TestClient, vapp: FastAPI) -> None:
    v = create(api, code="FLASH", max_uses=5)
    for i in range(20):
        user(api, f"buyer-{i}")
    settings = vapp.state.settings

    def use(i: int) -> str:
        with vapp.state.session_factory() as s:
            try:
                VoucherService(s, settings).redeem(
                    RedeemRequest(**order("FLASH", f"buyer-{i}")), OWNER_CTX
                )
                return "ok"
            except ConflictError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(use, range(20)))
    assert results.count("ok") == 5 and results.count("voucher_exhausted") == 15
    final = api.get(f"{C}/{v['id']}", headers=OWNER).json()
    assert (final["use_count"], final["status"], final["remaining_uses"]) == (5, "exhausted", 0)


def test_concurrent_redemptions_respect_max_unique_users(api: TestClient, vapp: FastAPI) -> None:
    v = create(api, code="FIRST3", max_unique_users=3, max_uses_per_user=5)
    for i in range(20):
        user(api, f"fan-{i}")
    settings = vapp.state.settings

    def use(i: int) -> str:
        with vapp.state.session_factory() as s:
            try:
                VoucherService(s, settings).redeem(
                    RedeemRequest(**order("FIRST3", f"fan-{i}")), OWNER_CTX
                )
                return "ok"
            except ConflictError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(use, range(20)))
    assert results.count("ok") == 3 and results.count("voucher_unique_user_limit") == 17
    winners = [f"fan-{i}" for i, r in enumerate(results) if r == "ok"]
    # A user who already has the voucher can use it again without taking a new slot
    assert redeem(api, "FIRST3", winners[0]).status_code == 200
    final = api.get(f"{C}/{v['id']}", headers=OWNER).json()
    assert (final["unique_user_count"], final["remaining_unique_users"], final["use_count"]) == (
        3,
        0,
        4,
    )


# --- integration API -------------------------------------------------------------------------


def test_integration_token_registers_validates_and_redeems_only(
    api: TestClient, vapp: FastAPI
) -> None:
    create(api, code="APPCODE")
    assert api.post(f"{I}/users", json={"external_id": "app-user"}).status_code == 401
    bad = api.post(
        f"{I}/users",
        json={"external_id": "app-user"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert bad.status_code == 401
    user(api, "app-user", headers=APP)
    check = order("APPCODE", "app-user")
    check.pop("order_reference")
    checked = api.post(f"{I}/validate", json=check, headers=APP)
    assert checked.status_code == 200 and checked.json()["valid"] is True
    resp = redeem(api, "APPCODE", "app-user", headers=APP)
    assert resp.status_code == 200, resp.text
    assert api.get(f"{I}/capacity", headers=APP).json()["total_users"] == 1
    # The voucher token gives no dashboard access at all
    assert api.get(C, headers=APP).status_code == 401
    assert api.post(C, json=body_for(C), headers=APP).status_code == 401
    assert api.post(f"{V}/validate", json=check, headers=APP).status_code == 401
    with vapp.state.session_factory() as s:
        event = s.scalars(select(VoucherUsageEvent)).one()
        audit = s.scalars(select(AuditLog).where(AuditLog.action == "voucher.redeem")).one()
    assert (event.actor, event.source) == (VoucherActor.VOUCHER_API, VoucherEventSource.API)
    assert event.category is VoucherCategory.COURSE
    assert audit.actor is Actor.SYSTEM and audit.changes["via"] == "voucher_api"
    entry = api.get(f"{V}/audit", params={"action": "voucher.redeem"}, headers=OWNER).json()[
        "items"
    ][0]
    assert (entry["actor"], entry["via"]) == ("system", "voucher_api")


def test_integration_api_is_off_without_a_token(client: TestClient) -> None:
    resp = client.post(
        f"{I}/redeem",
        json=order("X", "y"),
        headers={"Authorization": "Bearer anything-at-all"},
    )
    assert resp.status_code == 401 and error(resp) == "voucher_api_disabled"


def test_integration_token_failures_are_rate_limited(api: TestClient) -> None:
    statuses = [
        api.get(f"{I}/capacity", headers={"Authorization": f"Bearer wrong-{i}"}).status_code
        for i in range(25)
    ]
    assert statuses[0] == 401 and statuses[-1] == 429
    assert api.get(f"{I}/capacity", headers=APP).status_code == 429  # same client, still blocked


def test_voucher_token_never_appears_in_responses(api: TestClient, vapp: FastAPI) -> None:
    create(api, code="SAFE")
    user(api, "s1", headers=APP)
    redeem(api, "SAFE", "s1", headers=APP)
    bodies = [
        api.get(p, headers=OWNER).text
        for p in (
            C,
            L,
            f"{V}/audit",
            f"{V}/analytics",
            f"{V}/summary",
            f"{V}/users",
            "/api/v1/settings",
        )
    ]
    assert all(API_TOKEN not in b for b in bodies)
    assert API_TOKEN in vapp.state.settings.secret_values()  # registered for log redaction


# --- summary and analytics -------------------------------------------------------------------


def _event(vapp: FastAPI, voucher_id: int, code: str, at: datetime, ok: bool = True) -> None:
    with vapp.state.session_factory() as s, s.begin():
        v = s.get(Voucher, voucher_id)
        assert v is not None
        s.add(
            VoucherUsageEvent(
                voucher_id=voucher_id,
                voucher_code=code,
                category=v.category,
                user_external_id="u",
                outcome=VoucherEventOutcome.SUCCESS if ok else VoucherEventOutcome.FAILED,
                failure_reason=None if ok else "exhausted",
                source=VoucherEventSource.API,
                actor=VoucherActor.VOUCHER_API,
                occurred_at=at,
            )
        )
        if ok:
            v.use_count += 1


def test_summary_cards_per_category_come_from_real_data(api: TestClient) -> None:
    now = datetime.now(UTC)
    empty = api.get(f"{V}/summary", headers=VIEWER).json()
    assert (empty["total_vouchers"], empty["total_redemptions"], empty["unique_users_reached"]) == (
        0,
        0,
        0,
    )
    assert empty["discount_given"] == [] and empty["category"] is None
    create(api, code="C-ACTIVE")
    create(api, code="C-SOON", valid_from=(now + timedelta(days=3)).isoformat())
    create(
        api,
        code="C-OLD",
        valid_from=(now - timedelta(days=3)).isoformat(),
        valid_until=(now - timedelta(days=1)).isoformat(),
    )
    create(api, L, code="L-ACTIVE", discount_type="fixed", discount_value="50")
    for ext in ("s1", "s2"):
        user(api, ext)
    redeem(api, "C-ACTIVE", "s1")
    redeem(api, "C-ACTIVE", "s1")
    redeem(api, "C-ACTIVE", "s2")
    redeem(api, "L-ACTIVE", "s2", category="live_tutor", item="pro")
    redeem(api, "C-OLD", "s1")  # failed: expired

    course = api.get(f"{V}/summary", params={"category": "course"}, headers=VIEWER).json()
    assert (
        course["total_vouchers"],
        course["active"],
        course["scheduled"],
        course["expired"],
        course["total_redemptions"],
        course["failed_redemptions"],
        course["unique_users_reached"],
        course["redemptions_today"],
    ) == (3, 1, 1, 1, 3, 1, 2, 3)
    assert course["discount_given"] == [{"currency": "INR", "amount": "300"}]
    tutor = api.get(f"{V}/summary", params={"category": "live_tutor"}, headers=VIEWER).json()
    assert (tutor["total_vouchers"], tutor["total_redemptions"], tutor["unique_users_reached"]) == (
        1,
        1,
        1,
    )
    assert tutor["discount_given"] == [{"currency": "INR", "amount": "50"}]
    both = api.get(f"{V}/summary", headers=VIEWER).json()
    assert (both["total_vouchers"], both["total_redemptions"], both["unique_users_reached"]) == (
        4,
        4,
        2,
    )
    assert (both["total_users"], both["user_limit"]) == (2, 1000)


def test_analytics_come_only_from_recorded_events(api: TestClient, vapp: FastAPI) -> None:
    empty = api.get(f"{V}/analytics", headers=VIEWER).json()
    assert empty["range_success"] == empty["range_failed"] == 0
    assert all(b["success"] == b["failed"] == 0 for b in empty["series"])  # no fake data
    assert len(empty["series"]) == 30 and empty["most_used"] == []

    a = create(api, code="ALPHA")
    b = create(api, code="BETA")
    create(api, code="GAMMA")
    locked = create(api, code="DELTA")
    tutor = create(api, L, code="TUTOR")
    api.post(f"{C}/{locked['id']}/lock", json={}, headers=OWNER)
    # 2026-10-15 20:00 UTC is 2026-10-16 01:30 in Asia/Kolkata: it belongs to the 16th
    _event(vapp, a["id"], "ALPHA", datetime(2026, 10, 15, 20, 0, tzinfo=UTC))
    _event(vapp, a["id"], "ALPHA", datetime(2026, 10, 16, 5, 0, tzinfo=UTC))
    _event(vapp, a["id"], "ALPHA", datetime(2026, 10, 14, 5, 0, tzinfo=UTC))
    _event(vapp, b["id"], "BETA", datetime(2026, 10, 16, 6, 0, tzinfo=UTC))
    _event(vapp, b["id"], "BETA", datetime(2026, 10, 16, 7, 0, tzinfo=UTC), ok=False)
    _event(vapp, tutor["id"], "TUTOR", datetime(2026, 10, 16, 8, 0, tzinfo=UTC))

    params = {
        "granularity": "day",
        "start": "2026-10-13T18:30:00Z",
        "end": "2026-10-16T18:30:00Z",
        "category": "course",
    }
    data = api.get(f"{V}/analytics", params=params, headers=VIEWER).json()
    assert data["timezone"] == "Asia/Kolkata" and data["category"] == "course"
    assert [(s["label"], s["success"], s["failed"]) for s in data["series"]] == [
        ("14 Oct", 1, 0),
        ("15 Oct", 0, 0),
        ("16 Oct", 3, 1),
    ]
    assert (data["range_success"], data["range_failed"]) == (4, 1)
    assert [r["code"] for r in data["most_used"]] == ["ALPHA", "BETA"]
    assert {r["code"] for r in data["least_used"][:2]} == {"DELTA", "GAMMA"}
    summary = data["summary"]
    assert (
        summary["total_vouchers"],
        summary["locked"],
        summary["unlocked"],
        summary["active"],
    ) == (4, 1, 3, 3)
    assert summary["total_redemptions"] == 4
    everything = api.get(
        f"{V}/analytics",
        params={k: v for k, v in params.items() if k != "category"},
        headers=VIEWER,
    ).json()
    assert everything["range_success"] == 5 and len(everything["per_voucher"]) == 5

    week = api.get(
        f"{V}/analytics", params={**params, "granularity": "week"}, headers=VIEWER
    ).json()
    assert sum(s["success"] for s in week["series"]) == 4 and week["series"][0]["label"].startswith(
        "Wk"
    )
    month = api.get(
        f"{V}/analytics", params={**params, "granularity": "month"}, headers=VIEWER
    ).json()
    assert [(s["label"], s["success"]) for s in month["series"]] == [("Oct 2026", 4)]
    only_beta = api.get(
        f"{V}/analytics", params={**params, "voucher_id": b["id"]}, headers=VIEWER
    ).json()
    assert only_beta["range_success"] == 1 and [r["code"] for r in only_beta["per_voucher"]] == [
        "BETA"
    ]

    # Click-through: the records behind the "16 Oct" bar
    bar = data["series"][2]
    events = api.get(
        f"{V}/usage-events",
        params={"start": bar["start"], "end": bar["end"], "category": "course"},
        headers=VIEWER,
    ).json()
    assert events["total"] == 4 and {e["voucher_code"] for e in events["items"]} == {
        "ALPHA",
        "BETA",
    }
    assert (
        api.get(
            f"{V}/analytics",
            params={
                "granularity": "day",
                "start": "2020-01-01T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            },
            headers=VIEWER,
        ).status_code
        == 422
    )


# --- audit -----------------------------------------------------------------------------------


def test_audit_log_is_searchable_and_read_only(api: TestClient) -> None:
    v = create(api, code="AUDITED")
    api.post(f"{C}/{v['id']}/lock", json={"reason": "check"}, headers=OWNER)
    api.post(f"{C}/{v['id']}/unlock", headers=OWNER)
    page = api.get(f"{V}/audit", headers=VIEWER).json()
    assert [i["action"] for i in page["items"]][:3] == [
        "voucher.unlock",
        "voucher.lock",
        "voucher.create",
    ]
    assert page["items"][1]["changes"]["reason"] == "check"
    assert page["items"][2]["changes"]["category"] == "course"
    assert (
        api.get(f"{V}/audit", params={"action": "voucher.lock"}, headers=VIEWER).json()["total"]
        == 1
    )
    assert api.get(f"{V}/audit", params={"q": "audited"}, headers=VIEWER).json()["total"] >= 1
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert api.get(f"{V}/audit", params={"start": future}, headers=VIEWER).json()["total"] == 0
    # No route can change or remove audit records: every audit operation is a GET, and
    # write attempts on the path are refused without touching the table.
    spec = api.app.openapi()  # type: ignore[attr-defined]
    assert {m for p, ops in spec["paths"].items() if "audit" in p for m in ops} == {"get"}
    before = page["total"]
    for method in ("put", "patch", "delete", "post"):
        assert getattr(api, method)(f"{V}/audit", headers=OWNER).status_code in (405, 422)
    assert api.get(f"{V}/audit", headers=VIEWER).json()["total"] == before


def test_users_list_search_and_delete_rules(api: TestClient) -> None:
    create(api, code="U1CODE")
    user(api, "keep-me")
    gone = user(api, "remove-me")
    redeem(api, "U1CODE", "keep-me")
    page = api.get(f"{V}/users", params={"q": "keep"}, headers=VIEWER).json()
    assert page["total"] == 1 and page["items"][0]["redemptions"] == 1
    keep_id = page["items"][0]["id"]
    blocked = api.delete(f"{V}/users/{keep_id}", headers=OWNER)
    assert blocked.status_code == 409 and error(blocked) == "user_has_usage"
    assert api.delete(f"{V}/users/{gone['id']}", headers=VIEWER).status_code == 403
    assert api.delete(f"{V}/users/{gone['id']}", headers=OWNER).status_code == 204
    assert api.get(f"{V}/capacity", headers=OWNER).json()["total_users"] == 1
