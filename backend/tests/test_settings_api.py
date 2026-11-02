from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApplicationSetting
from app.services.settings import _assert_storable

URL = "/api/v1/settings"


def test_default_display_timezone(client: TestClient, viewer_headers: dict) -> None:
    data = client.get(URL, headers=viewer_headers).json()
    assert data == {
        "display_timezone": "Asia/Kolkata",
        "supported_currencies": ["EUR", "GBP", "INR", "USD"],
    }


def test_update_display_timezone(client: TestClient, owner_headers: dict, session: Session) -> None:
    resp = client.put(URL, json={"display_timezone": "Europe/London"}, headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json()["display_timezone"] == "Europe/London"
    assert client.get(URL, headers=owner_headers).json()["display_timezone"] == "Europe/London"
    row = session.scalars(select(ApplicationSetting)).one()
    assert row.key == "display_timezone" and row.updated_by.value == "owner"


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "../../etc/passwd"])
def test_invalid_timezone(client: TestClient, owner_headers: dict, tz: str) -> None:
    resp = client.put(URL, json={"display_timezone": tz}, headers=owner_headers)
    assert resp.status_code == 422


def test_secret_like_keys_can_never_be_stored() -> None:
    for key in ("openai_api_key", "smtp_password", "unknown_key"):
        with pytest.raises(ValueError, match="may not be stored"):
            _assert_storable(key)
