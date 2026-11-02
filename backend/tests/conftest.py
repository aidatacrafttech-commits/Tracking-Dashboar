from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.migrations import upgrade_to_head
from app.main import create_app
from app.models import Provider

OWNER_TOKEN = "test-owner-token-0123456789-abcdefghijklmnop"
VIEWER_TOKEN = "test-viewer-token-0123456789-abcdefghijklmnop"


def make_settings(db_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": f"sqlite:///{db_path.as_posix()}",
        "dashboard_owner_token": OWNER_TOKEN,
        "dashboard_viewer_token": VIEWER_TOKEN,
        "cors_origins": ["http://localhost:5173"],
        "supported_currencies": ["USD", "INR", "EUR", "GBP"],
        "default_display_timezone": "Asia/Kolkata",
        "log_json": True,
        # Don't serve the real frontend build in API tests (tests opt in explicitly).
        "frontend_dist_dir": db_path.parent / "no-frontend-build",
        "allowed_hosts": ["testserver", "127.0.0.1", "localhost"],
    }
    values.update(overrides)
    # _env_file=None: never read the developer's real backend/.env during tests.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run migrations once; each test gets a fresh copy of the resulting file."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    upgrade_to_head(f"sqlite:///{path.as_posix()}")
    return path


@pytest.fixture
def db_path(tmp_path: Path, migrated_template: Path) -> Path:
    path = tmp_path / "test.db"
    shutil.copyfile(migrated_template, path)
    return path


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return make_settings(db_path)


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(app: FastAPI) -> Iterator[Session]:
    db: Session = app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def owner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


@pytest.fixture
def viewer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def provider_by_slug(session: Session, slug: str) -> Provider:
    provider = session.query(Provider).filter_by(slug=slug).one()
    return provider
