"""Application factory.

Run with:  uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100
Use a single worker: SQLite and (from Phase 2) the in-process scheduler assume one process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, register_error_handlers
from app.core.logging import configure_logging, register_secrets
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.core.security import AttemptLimiter, SessionManager
from app.db.migrations import current_revision, head_revision
from app.db.session import create_db_engine, create_session_factory
from app.notifications.outbox import OutboxDispatcher
from app.notifications.smtp import SmtpSender
from app.ops.service import OperationsService
from app.ops.storage import permission_findings
from app.providers.registry import ProviderRuntime, build_runtimes
from app.services.app_users import build_app_users_source
from app.services.course_storage import build_course_storage
from app.services.health import ReadinessService
from app.services.polling import PollingCoordinator, PollingScheduler
from app.services.presence import PresenceTracker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    runtimes: dict[str, ProviderRuntime] | None = None,
    start_scheduler: bool | None = None,
    smtp_sender: SmtpSender | None = None,
) -> FastAPI:
    """``runtimes`` lets tests inject adapters backed by mocked transports/clients."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    register_secrets(*settings.secret_values())

    database_url = settings.resolved_database_url
    engine = create_db_engine(database_url)
    expected_revision = head_revision(database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            revision = current_revision(engine)
        except Exception:
            revision = None
            logger.exception("database not reachable at startup")
        if revision != expected_revision:
            # Don't auto-migrate: schema changes stay an explicit, reviewed step.
            logger.warning(
                "database schema is not at head; run 'alembic upgrade head'",
                extra={"current_revision": revision, "head_revision": expected_revision},
            )
        scheduler: PollingScheduler | None = None
        run_scheduler = (
            start_scheduler
            if start_scheduler is not None
            else settings.polling_enabled and settings.app_env != "test"
        )
        if run_scheduler and revision == expected_revision:
            scheduler = PollingScheduler(
                app.state.coordinator,
                dispatcher=app.state.dispatcher,
                operations=app.state.operations,
                session_manager=app.state.session_manager,
            )
            await scheduler.start()
        app.state.scheduler = scheduler
        findings = permission_findings(settings)
        if findings:
            logger.warning(
                "sensitive files are accessible to other accounts; run scripts/harden_permissions",
                extra={"files": [f.path for f in findings]},
            )
        logger.info(
            "application started",
            extra={
                "env": settings.app_env,
                "version": settings.app_version,
                "polling": scheduler is not None,
                "providers": {slug: rt.status.value for slug, rt in app.state.runtimes.items()},
            },
        )
        yield
        # Graceful shutdown: end live streams, cancel background tasks (in-flight runs are
        # recorded as interrupted and their locks released), then close clients and the DB.
        app.state.shutting_down.set()
        if scheduler is not None:
            await scheduler.stop()
        await app.state.coordinator.aclose()
        engine.dispose()
        if app.state.app_users_source is not None:
            app.state.app_users_source.close()
        logger.info("application stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.readiness = ReadinessService(engine, expected_revision)
    # A misconfigured provider becomes NOT_CONFIGURED here; it never stops startup.
    app.state.runtimes = runtimes if runtimes is not None else build_runtimes(settings)
    app.state.coordinator = PollingCoordinator(
        app.state.session_factory, app.state.runtimes, settings
    )
    app.state.session_manager = SessionManager(app.state.session_factory, settings)
    app.state.operations = OperationsService(app.state.session_factory, settings)
    app.state.shutting_down = asyncio.Event()
    app.state.scheduler = None
    app.state.attempt_limiter = AttemptLimiter(app.state.session_factory, settings)
    # Course files (S3). None = not configured: uploads are refused, video URLs still work.
    app.state.course_storage = build_course_storage(settings)
    # App Users (AdhyaapakAI MongoDB, read-only). None = not configured; connects lazily.
    app.state.app_users_source = build_app_users_source(settings)
    # Who has the mobile app open right now (heartbeats; in memory, one worker).
    app.state.presence = PresenceTracker()
    app.state.smtp_sender = smtp_sender or SmtpSender(settings)
    app.state.dispatcher = OutboxDispatcher(
        app.state.session_factory, settings, app.state.smtp_sender
    )

    register_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,  # bearer tokens, not cookies
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    # Unknown Host headers are refused (defence against DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    # Added last so it is outermost: request ids cover CORS and error responses too.
    app.add_middleware(RequestContextMiddleware, asset_origin=settings.course_assets_origin)
    app.include_router(api_router)
    mount_frontend(app, settings.frontend_dist_dir)
    return app


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built dashboard (if present) with SPA fallback to index.html."""
    index = dist / "index.html"
    if not index.is_file():
        logger.info("frontend build not found; serving API only", extra={"path": str(dist)})
        return
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # API and API-docs paths are never answered with the UI (docs stay off in production).
        if path.startswith("api/") or path in ("docs", "redoc", "openapi.json"):
            raise NotFoundError("Not Found")
        candidate = (root / path).resolve()
        # Never serve anything outside the build directory (path traversal guard).
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)
