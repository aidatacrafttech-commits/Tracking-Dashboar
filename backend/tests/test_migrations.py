from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

import app.models  # noqa: F401
from app.db.base import Base
from app.db.migrations import alembic_config, current_revision, head_revision, upgrade_to_head
from app.db.session import create_db_engine

EXPECTED_TABLES = {
    "providers",
    "provider_balance_snapshots",
    "provider_usage_snapshots",
    "payment_transactions",
    "threshold_configs",
    "alert_events",
    "monitoring_runs",
    "service_health_checks",
    "audit_logs",
    "application_settings",
    "provider_sync_states",
    "infrastructure_resource_snapshots",
    "alert_delivery_attempts",
    "auth_sessions",
    "auth_attempts",
    "job_locks",
    "vouchers",
    "voucher_users",
    "voucher_usage_events",
    "voucher_targets",
    "course_taxonomy_terms",
    "course_assets",
    "courses",
    "course_playlists",
    "course_chapters",
    "course_lectures",
    "course_lecture_variants",
    "course_lecture_resources",
    "course_enrollments",
    "course_enrollment_events",
    "alembic_version",
}


def test_upgrade_creates_schema_and_seeds_providers(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade_to_head(url)
    engine = create_db_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
        assert current_revision(engine) == head_revision(url)
        with engine.connect() as conn:
            slugs = {r[0] for r in conn.execute(text("SELECT slug FROM providers"))}
            fk_on = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert slugs == {"openai", "sarvam", "deepgram", "elevenlabs", "aws"}
        assert fk_on == 1
    finally:
        engine.dispose()


def test_models_match_migrations(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade_to_head(url)
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"models and migrations drifted: {diff}"
    finally:
        engine.dispose()


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_db_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")


def test_0007_keeps_existing_vouchers_usable(tmp_path: Path) -> None:
    """Vouchers created before categories existed become Course vouchers for all subjects
    with no discount (what they allowed before), and history gets its category and
    unique-user counts."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0006")
    engine = create_db_engine(url)
    now = "2026-10-20 10:00:00.000000"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO vouchers (id, code, name, is_active, use_count, is_locked,"
                    " created_by, updated_by, version, created_at, updated_at)"
                    " VALUES (1, 'OLD1', 'Old', 1, 3, 0, 'owner', 'owner', 1, :t, :t)"
                ),
                {"t": now},
            )
            for uid in (1, 2):
                conn.execute(
                    text(
                        "INSERT INTO voucher_users (id, external_id, created_at, created_by)"
                        " VALUES (:i, :e, :t, 'owner')"
                    ),
                    {"i": uid, "e": f"user-{uid}", "t": now},
                )
            for uid, outcome in ((1, "success"), (1, "success"), (2, "success"), (2, "failed")):
                conn.execute(
                    text(
                        "INSERT INTO voucher_usage_events (voucher_id, voucher_code,"
                        " voucher_user_id, outcome, source, actor, occurred_at)"
                        " VALUES (1, 'OLD1', :u, :o, 'dashboard', 'owner', :t)"
                    ),
                    {"u": uid, "o": outcome, "t": now},
                )
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT category, discount_type, discount_value, currency, applies_to_all,"
                    " unique_user_count FROM vouchers WHERE id = 1"
                )
            ).one()
            categories = {
                r[0] for r in conn.execute(text("SELECT category FROM voucher_usage_events"))
            }
        assert tuple(row) == ("course", "percentage", 0, "INR", 1, 2)
        assert categories == {"course"}
    finally:
        engine.dispose()
