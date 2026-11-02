from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import OWNER_TOKEN, make_settings


def test_valid_settings_hide_secrets(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "x.db")
    assert OWNER_TOKEN not in repr(settings)
    assert OWNER_TOKEN not in str(settings.model_dump())


def test_short_owner_token_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make_settings(tmp_path / "x.db", dashboard_owner_token="too-short")


def test_viewer_token_must_differ_from_owner(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must differ"):
        make_settings(tmp_path / "x.db", dashboard_viewer_token=OWNER_TOKEN)


def test_empty_viewer_token_disables_viewer(tmp_path: Path) -> None:
    assert (
        make_settings(tmp_path / "x.db", dashboard_viewer_token="").dashboard_viewer_token is None
    )


def test_currencies_parsed_from_csv_and_validated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "x.db", supported_currencies="usd, inr")
    assert settings.supported_currencies == ["INR", "USD"]
    with pytest.raises(ValidationError, match="invalid ISO 4217"):
        make_settings(tmp_path / "x.db", supported_currencies="US1")


def test_invalid_timezone_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        make_settings(tmp_path / "x.db", default_display_timezone="Mars/Base")


def test_production_forces_docs_off_and_rejects_wildcard_cors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "x.db", app_env="production", docs_enabled=True)
    assert settings.docs_enabled is False
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        make_settings(tmp_path / "x.db", app_env="production", cors_origins="*")


def test_relative_sqlite_path_anchored_to_backend_dir(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "x.db", database_url="sqlite:///./data/monitor.db")
    assert settings.resolved_database_url.endswith("/backend/data/monitor.db")


def test_env_example_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import BACKEND_DIR, Settings

    example = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        example.replace("DASHBOARD_OWNER_TOKEN=", f"DASHBOARD_OWNER_TOKEN={OWNER_TOKEN}")
    )
    for name in ("DATABASE_URL", "DASHBOARD_OWNER_TOKEN", "AWS_PROFILE", "AWS_REGIONS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.aws_monitoring_enabled is False
    assert settings.aws_regions == ["ap-south-1"]
    assert settings.poll_interval_minutes == 30
    assert settings.openai_admin_key is None and settings.disabled_providers == []


def test_extra_env_files_have_lower_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import config

    other = tmp_path / "other.env"
    other.write_text("ELEVENLABS_API_KEY=from-other-file-0123456789\nCASHFREE_SECRET_KEY=ignored\n")
    primary = tmp_path / "primary.env"
    primary.write_text(f"DASHBOARD_OWNER_TOKEN={OWNER_TOKEN}\nPOLL_INTERVAL_MINUTES=45\n")
    monkeypatch.setenv("EXTRA_ENV_FILES", str(other))
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    files = config.env_files()
    assert files[0] == other
    monkeypatch.delenv("DASHBOARD_OWNER_TOKEN", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    settings = config.Settings(_env_file=(other, primary))  # type: ignore[call-arg]
    assert settings.elevenlabs_api_key is not None
    assert settings.elevenlabs_api_key.get_secret_value() == "from-other-file-0123456789"
    assert settings.poll_interval_minutes == 45
    assert not hasattr(settings, "cashfree_secret_key")


def test_provider_secrets_registered_for_redaction(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path / "x.db",
        openai_admin_key="sk-admin-abcdefghijklmnop",
        aws_access_key_id="AKIAABCDEFGHIJKLMNOP",
        aws_secret_access_key="secret-access-key-value-123",
    )
    secrets = settings.secret_values()
    assert "sk-admin-abcdefghijklmnop" in secrets and "secret-access-key-value-123" in secrets
    with pytest.raises(ValidationError, match="both AWS_ACCESS_KEY_ID"):
        make_settings(tmp_path / "x.db", aws_access_key_id="AKIAABCDEFGHIJKLMNOP")
    with pytest.raises(ValidationError, match="invalid AWS region"):
        make_settings(tmp_path / "x.db", aws_regions="mumbai")


def test_email_settings_validation(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    assert make_settings(db).email_delivery_enabled is False  # safe default
    with pytest.raises(ValidationError, match="requires SMTP_HOST and SMTP_FROM"):
        make_settings(db, email_delivery_enabled=True)
    with pytest.raises(ValidationError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        make_settings(db, smtp_username="mailer")
    with pytest.raises(ValidationError, match="invalid alert recipient"):
        make_settings(db, alert_recipients="ops@example.com, nope")
    with pytest.raises(ValidationError, match="EXCHANGE_RATE_SOURCE"):
        make_settings(db, exchange_rates="USD/INR=83.2")
    with pytest.raises(ValidationError, match="local relay"):
        make_settings(
            db,
            app_env="production",
            cors_origins=[],
            smtp_host="smtp.example.com",
            smtp_from="m@example.com",
            smtp_security="none",
        )
    ok = make_settings(
        db,
        exchange_rates="usd/inr=83.25",
        exchange_rate_source="RBI 2026-10-12",
        alert_recipients="Ops@Example.com,ops@example.com",
        smtp_password="pw-123",
        smtp_username="mailer",
    )
    assert ok.exchange_rates == {"USD/INR": Decimal("83.25")}
    assert ok.alert_recipients == ["ops@example.com"]
    assert "pw-123" in ok.secret_values()
