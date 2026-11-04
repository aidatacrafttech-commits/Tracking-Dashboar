"""Typed application settings loaded from environment variables / backend/.env.

Secrets are held as ``SecretStr`` so they never appear in reprs, logs or API output.
Nothing in this module is ever persisted to the database.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+$")
MIN_TOKEN_LENGTH = 32


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


_ENV_CONFIG = SettingsConfigDict(
    env_file=BACKEND_DIR / ".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


def _resolve_sqlite_url(url: str) -> str:
    """Anchor relative SQLite paths at the backend directory, not the process cwd."""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        raw_path = url[len(prefix) :]
        if raw_path and raw_path != ":memory:" and not Path(raw_path).is_absolute():
            return prefix + (BACKEND_DIR / raw_path).resolve().as_posix()
    return url


class DatabaseSettings(BaseSettings):
    """Just what migrations need, so `alembic upgrade` works without any secrets set."""

    model_config = _ENV_CONFIG

    database_url: str = "sqlite:///./data/monitor.db"

    @property
    def resolved_database_url(self) -> str:
        return _resolve_sqlite_url(self.database_url)


class Settings(BaseSettings):
    model_config = _ENV_CONFIG

    app_name: str = "Adhyaapak AI Dashboard"
    app_version: str = "0.1.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    docs_enabled: bool = True

    database_url: str = "sqlite:///./data/monitor.db"

    dashboard_owner_token: SecretStr = Field(..., description="Full-access bearer token")
    dashboard_viewer_token: SecretStr | None = Field(
        default=None, description="Optional read-only bearer token"
    )

    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    supported_currencies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["USD", "INR", "EUR", "GBP"]
    )
    default_display_timezone: str = "UTC"

    # --- Polling (Phase 2) -------------------------------------------------------
    polling_enabled: bool = True
    poll_interval_minutes: int = Field(default=30, ge=5, le=1440)
    health_check_interval_minutes: int = Field(default=5, ge=1, le=1440)
    # Cost Explorer bills $0.01 per request and refreshes at most a few times a day.
    aws_cost_explorer_interval_minutes: int = Field(default=360, ge=60, le=1440)
    usage_lookback_days: int = Field(default=31, ge=1, le=90)
    http_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    http_max_retries: int = Field(default=2, ge=0, le=5)
    operation_timeout_seconds: float = Field(default=120.0, ge=10.0, le=900.0)
    disabled_providers: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Provider credentials (never stored, never returned) -----------------------
    openai_admin_key: SecretStr | None = None
    deepgram_api_key: SecretStr | None = None
    deepgram_project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")
    elevenlabs_api_key: SecretStr | None = None

    # AWS: explicit opt-in. Credentials come from these variables when set, otherwise
    # from the standard boto3 chain (AWS_PROFILE, instance role, ...).
    aws_monitoring_enabled: bool = False
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None
    aws_profile: str | None = None
    aws_regions: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["ap-south-1"])
    aws_cost_explorer_enabled: bool = True
    aws_credits_enabled: bool = True
    aws_resources_enabled: bool = True

    # --- Dashboard UI (Phase 4) ---------------------------------------------------
    # Built frontend served at "/" when present (same origin: no CORS needed).
    frontend_dist_dir: Path = BACKEND_DIR.parent / "frontend" / "dist"
    sse_check_seconds: float = Field(default=5.0, ge=0.05, le=60.0)
    sse_max_connection_seconds: float = Field(default=300.0, ge=0.1, le=3600.0)

    # --- Security hardening (Phase 5) ----------------------------------------------
    # Browser sessions: HttpOnly cookie + server-side record; CSRF token required for writes.
    session_idle_minutes: int = Field(default=60, ge=5, le=1440)
    session_max_hours: int = Field(default=12, ge=1, le=72)
    session_remember_days: int = Field(default=7, ge=1, le=30)
    # None = secure cookies everywhere except APP_ENV=development/test over plain HTTP.
    session_cookie_secure: bool | None = None
    # Bearer tokens in the Authorization header (scripts/automation). Browsers use sessions.
    bearer_auth_enabled: bool = True
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)
    # Host header allow-list (blocks DNS rebinding). Hostnames only, no ports.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost"]
    )
    max_request_bytes: int = Field(default=65_536, ge=1024, le=10_485_760)
    # Backups and storage
    backup_dir: Path = BACKEND_DIR / "backups"
    backup_interval_hours: float = Field(default=24, ge=1, le=168)
    backup_keep: int = Field(default=14, ge=1, le=365)
    disk_min_free_mb: int = Field(default=200, ge=10, le=1_000_000)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _parse_hosts(cls, value: object) -> object:
        value = _split_csv(value)
        if isinstance(value, list):
            value = [str(v).strip().lower() for v in value]
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_hosts(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("ALLOWED_HOSTS must list at least one host")
        for host in value:
            if not re.fullmatch(r"(\*\.)?[a-z0-9.-]+|\[[0-9a-f:]+\]", host):
                raise ValueError(f"invalid host in ALLOWED_HOSTS: {host!r}")
        return value

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.app_env == "production"

    # --- Alerts and email (Phase 3) -----------------------------------------------
    # Master switch. While false, alerts are evaluated and recorded but never emailed
    # (only the explicit test-email action can send).
    email_delivery_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    alert_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    dashboard_url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    alert_max_delivery_attempts: int = Field(default=6, ge=1, le=20)
    alert_retry_base_seconds: int = Field(default=60, ge=5, le=3600)
    alert_retry_max_seconds: int = Field(default=3600, ge=60, le=86400)
    # Optional currency conversion. Off unless both a rate table and its source are given,
    # e.g. EXCHANGE_RATES="USD/INR=83.25" and EXCHANGE_RATE_SOURCE="RBI reference rate 2026-10-12".
    exchange_rates: Annotated[dict[str, Decimal], NoDecode] = Field(default_factory=dict)
    exchange_rate_source: str | None = None

    # --- Voucher management ---------------------------------------------------------
    # Bearer token for your app/scripts to register voucher users and redeem vouchers.
    # It grants nothing else (no dashboard access). Unset = the integration API is off.
    voucher_api_token: SecretStr | None = None
    # Starting user limit; the owner can change it at runtime (stored in the database).
    voucher_default_user_limit: int = Field(default=1000, ge=1, le=10_000_000)

    # --- AdhyaapakAI MongoDB (App Users screen) -----------------------------------------
    # Read-only analytics over the app's users: the dashboard SERVER connects; the URL never
    # reaches the browser or the logs. Use an Atlas user with only the "read" role on this
    # database. Both empty = the screen shows "not configured".
    adhyaapak_mongo_url: SecretStr | None = None
    adhyaapak_mongo_db: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # Live "online now": the mobile app sends heartbeats signed with its own login token; the
    # dashboard verifies them with the AdhyaapakAI backend's JWT_SECRET. Unset = presence off.
    adhyaapak_jwt_secret: SecretStr | None = None

    # --- Course management ----------------------------------------------------------
    # Bearer token for the trusted app backend (it verifies payments and authenticates
    # students) to confirm payments, enroll students and check access. Unset = API off.
    course_api_token: SecretStr | None = None
    # S3 bucket for course files (thumbnails, videos, audio, subtitles, PDFs). Separate
    # from the read-only AWS monitoring credentials: these keys need s3:PutObject and
    # s3:GetObject on this bucket only. Unset bucket = uploads off (video URLs still work).
    course_assets_bucket: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
    )
    course_assets_region: str = Field(default="ap-south-1", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    # Objects go to {prefix}{course_id}/{asset_id}-{file}. Never under videos/ or notes/: the
    # AdhyaapakAI backend's S3 sync parses those keys if the bucket is ever shared with it.
    course_assets_prefix: str = Field(
        default="course-assets/", pattern=r"^[A-Za-z0-9!_.*'()-][A-Za-z0-9!_.*'()/-]{0,198}/$"
    )
    course_assets_access_key_id: SecretStr | None = None
    course_assets_secret_access_key: SecretStr | None = None
    course_assets_url_ttl_seconds: int = Field(default=900, ge=60, le=43_200)
    course_asset_max_video_mb: int = Field(default=4096, ge=1, le=51_200)
    course_asset_max_file_mb: int = Field(default=100, ge=1, le=2048)

    @property
    def course_assets_origin(self) -> str | None:
        """The bucket's HTTPS origin (for the UI's CSP: uploads, previews, playback)."""
        if not self.course_assets_bucket:
            return None
        return f"https://{self.course_assets_bucket}.s3.{self.course_assets_region}.amazonaws.com"

    @field_validator("course_assets_prefix")
    @classmethod
    def _course_prefix(cls, value: str) -> str:
        if value.lower().startswith(("videos/", "notes/")):
            raise ValueError("course files cannot live under videos/ or notes/")
        return value

    @field_validator("alert_recipients", mode="before")
    @classmethod
    def _parse_recipients(cls, value: object) -> object:
        value = _split_csv(value)
        if isinstance(value, list):
            value = [str(v).strip().lower() for v in value]
        return value

    @field_validator("alert_recipients")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        for email in value:
            if not _EMAIL_RE.match(email):
                raise ValueError(f"invalid alert recipient: {email!r}")
        return list(dict.fromkeys(value))

    @field_validator("smtp_from")
    @classmethod
    def _validate_from(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL_RE.match(value.strip().lower()):
            raise ValueError("SMTP_FROM must be an email address")
        return value.strip() if value else value

    @field_validator("exchange_rates", mode="before")
    @classmethod
    def _parse_rates(cls, value: object) -> object:
        if isinstance(value, str):
            rates: dict[str, Decimal] = {}
            for item in _split_csv(value):  # type: ignore[union-attr]
                pair, _, rate = str(item).partition("=")
                rates[pair.strip().upper()] = Decimal(rate.strip())
            return rates
        return value

    @field_validator("exchange_rates")
    @classmethod
    def _validate_rates(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        for pair, rate in value.items():
            if not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", pair):
                raise ValueError(f"exchange rate pair must look like USD/INR, got {pair!r}")
            if not rate.is_finite() or rate <= 0:
                raise ValueError(f"exchange rate for {pair} must be positive")
        return value

    @field_validator(
        "openai_admin_key",
        "deepgram_api_key",
        "elevenlabs_api_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "deepgram_project_id",
        "aws_profile",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "dashboard_url",
        "exchange_rate_source",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("disabled_providers", "aws_regions", mode="before")
    @classmethod
    def _parse_lower_csv(cls, value: object) -> object:
        value = _split_csv(value)
        if isinstance(value, list):
            value = [str(item).strip().lower() for item in value]
        return value

    @field_validator("aws_regions")
    @classmethod
    def _validate_regions(cls, value: list[str]) -> list[str]:
        for region in value:
            if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
                raise ValueError(f"invalid AWS region: {region!r}")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("supported_currencies", mode="before")
    @classmethod
    def _parse_currencies(cls, value: object) -> object:
        value = _split_csv(value)
        if isinstance(value, list):
            value = [str(item).upper() for item in value]
        return value

    @field_validator("supported_currencies")
    @classmethod
    def _validate_currencies(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one supported currency is required")
        for code in value:
            if not _CURRENCY_RE.match(code):
                raise ValueError(f"invalid ISO 4217 currency code: {code!r}")
        return sorted(set(value))

    @field_validator("default_display_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator(
        "dashboard_viewer_token",
        "voucher_api_token",
        "course_api_token",
        "course_assets_bucket",
        "adhyaapak_mongo_url",
        "adhyaapak_mongo_db",
        "adhyaapak_jwt_secret",
        "course_assets_access_key_id",
        "course_assets_secret_access_key",
        mode="before",
    )
    @classmethod
    def _empty_viewer_token_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "dashboard_owner_token", "dashboard_viewer_token", "voucher_api_token", "course_api_token"
    )
    @classmethod
    def _validate_token_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Settings:
        if self.email_delivery_enabled and not (self.smtp_host and self.smtp_from):
            raise ValueError("EMAIL_DELIVERY_ENABLED=true requires SMTP_HOST and SMTP_FROM")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("set both SMTP_USERNAME and SMTP_PASSWORD, or neither")
        if self.exchange_rates and not self.exchange_rate_source:
            raise ValueError("EXCHANGE_RATES requires EXCHANGE_RATE_SOURCE (documented origin)")
        if (
            self.app_env == "production"
            and self.smtp_security == "none"
            and self.smtp_host not in (None, "localhost", "127.0.0.1")
        ):
            raise ValueError("SMTP_SECURITY=none is only allowed for a local relay in production")
        if (self.aws_access_key_id is None) != (self.aws_secret_access_key is None):
            raise ValueError("set both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or neither")
        if (
            self.dashboard_viewer_token is not None
            and self.dashboard_viewer_token.get_secret_value()
            == self.dashboard_owner_token.get_secret_value()
        ):
            raise ValueError("DASHBOARD_VIEWER_TOKEN must differ from DASHBOARD_OWNER_TOKEN")
        if self.voucher_api_token is not None and self.voucher_api_token.get_secret_value() in {
            self.dashboard_owner_token.get_secret_value(),
            self.dashboard_viewer_token.get_secret_value() if self.dashboard_viewer_token else None,
        }:
            raise ValueError("VOUCHER_API_TOKEN must differ from the dashboard tokens")
        if self.course_api_token is not None and self.course_api_token.get_secret_value() in {
            self.dashboard_owner_token.get_secret_value(),
            self.dashboard_viewer_token.get_secret_value() if self.dashboard_viewer_token else None,
            self.voucher_api_token.get_secret_value() if self.voucher_api_token else None,
        }:
            raise ValueError("COURSE_API_TOKEN must differ from the other tokens")
        if (self.course_assets_access_key_id is None) != (
            self.course_assets_secret_access_key is None
        ):
            raise ValueError(
                "set both COURSE_ASSETS_ACCESS_KEY_ID and COURSE_ASSETS_SECRET_ACCESS_KEY,"
                " or neither"
            )
        if (self.adhyaapak_mongo_url is None) != (self.adhyaapak_mongo_db is None):
            raise ValueError("set both ADHYAAPAK_MONGO_URL and ADHYAAPAK_MONGO_DB, or neither")
        if self.adhyaapak_mongo_url is not None and not re.match(
            r"^mongodb(\+srv)?://\S+$", self.adhyaapak_mongo_url.get_secret_value()
        ):
            raise ValueError("ADHYAAPAK_MONGO_URL must start with mongodb:// or mongodb+srv://")
        if self.app_env == "production":
            # API schema is not needed in production and widens the attack surface.
            self.docs_enabled = False
            if self.session_cookie_secure is False:
                raise ValueError("SESSION_COOKIE_SECURE=false is not allowed in production")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS must not contain '*' in production")
        return self

    def secret_values(self) -> list[str]:
        """Raw secret strings, used only to register them with the log redactor."""
        candidates = [
            self.dashboard_owner_token,
            self.dashboard_viewer_token,
            self.openai_admin_key,
            self.deepgram_api_key,
            self.elevenlabs_api_key,
            self.aws_access_key_id,
            self.aws_secret_access_key,
            self.aws_session_token,
            self.smtp_password,
            self.voucher_api_token,
            self.course_api_token,
            self.course_assets_access_key_id,
            self.course_assets_secret_access_key,
            self.adhyaapak_mongo_url,
            self.adhyaapak_jwt_secret,
        ]
        values = [c.get_secret_value() for c in candidates if c is not None]
        # The redactor has no pattern for URI userinfo, so also register the bare password
        # (a driver error could quote it without the rest of the URL).
        if self.adhyaapak_mongo_url is not None:
            userinfo = urlsplit(self.adhyaapak_mongo_url.get_secret_value()).password
            if userinfo:
                values += [userinfo, unquote(userinfo)]
        return values

    @property
    def resolved_database_url(self) -> str:
        return _resolve_sqlite_url(self.database_url)


def env_files() -> tuple[Path, ...]:
    """backend/.env plus optional EXTRA_ENV_FILES (comma-separated paths).

    Extra files are read first, so backend/.env always wins on conflicts. Only the
    variable names this service defines are read; anything else in them is ignored.
    """
    extra = [
        Path(item.strip()).expanduser()
        for item in os.environ.get("EXTRA_ENV_FILES", "").split(",")
        if item.strip()
    ]
    return (*extra, BACKEND_DIR / ".env")


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment / env files.
    return Settings(_env_file=env_files())  # type: ignore[call-arg]
