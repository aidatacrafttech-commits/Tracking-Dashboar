"""Operational status, backups and readiness checks (owner-visible, never secret)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.time import utcnow
from app.db.session import transaction
from app.models import AlertEvent, AuthAttempt, AuthSession, MonitoringRun
from app.models.enums import DeliveryStatus, RunStatus, RunTrigger
from app.ops.locks import JobLocks
from app.ops.storage import BackupError, disk_status, permission_findings, run_backup, sqlite_path
from app.schemas.common import ApiModel

logger = logging.getLogger(__name__)

BACKUP_JOB = "backup"
BACKUP_LOCK_TTL = timedelta(minutes=30)


class BackupInfo(ApiModel):
    enabled: bool
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_status: str | None
    last_error: str | None
    overdue: bool


class OpsStatusRead(ApiModel):
    status: Literal["ok", "warning", "critical"]
    problems: list[str]
    disk_free_mb: int
    disk_status: str
    backup: BackupInfo
    permission_findings: list[str]
    scheduler: str
    active_sessions: int
    failed_logins_last_window: int
    failed_alert_emails: int


class OperationsService:
    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.locks = JobLocks(session_factory)

    # --- backups ---------------------------------------------------------------------------
    def backup_due(self) -> bool:
        info = self.backup_info()
        return info.enabled and (info.last_success_at is None or info.overdue)

    def backup_info(self) -> BackupInfo:
        enabled = sqlite_path(self.settings) is not None
        session = self.session_factory()
        try:
            runs = list(
                session.scalars(
                    select(MonitoringRun)
                    .where(MonitoringRun.job_name == BACKUP_JOB)
                    .order_by(MonitoringRun.started_at.desc())
                    .limit(20)
                )
            )
        finally:
            session.close()
        last = runs[0] if runs else None
        success = next((r for r in runs if r.status is RunStatus.SUCCEEDED), None)
        interval = timedelta(hours=self.settings.backup_interval_hours)
        overdue = enabled and (
            success is None
            or utcnow() - (success.finished_at or success.started_at) > interval * 1.5
        )
        return BackupInfo(
            enabled=enabled,
            last_success_at=success.finished_at if success else None,
            last_attempt_at=last.started_at if last else None,
            last_status=last.status.value if last else None,
            last_error=last.error_message if last and last.status is RunStatus.FAILED else None,
            overdue=overdue,
        )

    def run_backup(self, trigger: RunTrigger) -> MonitoringRun:
        """Back up the database once; the outcome is always recorded as a monitoring run."""
        if not self.locks.acquire(BACKUP_JOB, BACKUP_LOCK_TTL):
            raise BackupError("a backup is already running")
        session = self.session_factory()
        try:
            with transaction(session):
                run = MonitoringRun(job_name=BACKUP_JOB, trigger=trigger, status=RunStatus.RUNNING)
                session.add(run)
                session.flush()
            try:
                result = run_backup(self.settings, run_id=run.id)
            except BackupError as exc:
                logger.critical("database backup failed", extra={"error": str(exc)})
                with transaction(session):
                    run.status = RunStatus.FAILED
                    run.finished_at = utcnow()
                    run.error_type = "BackupError"
                    run.error_message = str(exc)[:1000]
                return run
            with transaction(session):
                run.status = RunStatus.SUCCEEDED
                run.finished_at = utcnow()
                run.records_written = 1
                run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)
            logger.info(
                "database backup written", extra={"file": result.file, "bytes": result.size_bytes}
            )
            return run
        finally:
            session.close()
            self.locks.release(BACKUP_JOB)

    # --- status ----------------------------------------------------------------------------------
    def status(self, scheduler_state: str) -> OpsStatusRead:
        disk = disk_status(self.settings)
        backup = self.backup_info()
        perms = [f"{f.path}: {f.problem}" for f in permission_findings(self.settings)]
        session = self.session_factory()
        try:
            now = utcnow()
            active = (
                session.scalar(
                    select(func.count())
                    .select_from(AuthSession)
                    .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
                )
                or 0
            )
            failed_logins = (
                session.scalar(
                    select(func.count())
                    .select_from(AuthAttempt)
                    .where(
                        AuthAttempt.success.is_(False),
                        AuthAttempt.occurred_at
                        >= now - timedelta(minutes=self.settings.login_window_minutes),
                    )
                )
                or 0
            )
            failed_emails = (
                session.scalar(
                    select(func.count())
                    .select_from(AlertEvent)
                    .where(AlertEvent.delivery_status == DeliveryStatus.FAILED)
                )
                or 0
            )
        finally:
            session.close()

        problems: list[str] = []
        critical = False
        if disk.status == "failed":
            critical = True
            problems.append(f"Disk almost full: {disk.free_mb} MB free")
        elif disk.status == "warning":
            problems.append(f"Disk space low: {disk.free_mb} MB free")
        if backup.enabled and backup.last_status == "failed":
            problems.append(f"Last backup failed: {backup.last_error}")
        if backup.overdue:
            problems.append("No recent successful database backup")
        if perms:
            problems.append("Secrets or data files are readable by other accounts")
        if scheduler_state == "stopped":
            critical = True
            problems.append("Background monitoring is not running")
        if failed_emails:
            problems.append(f"{failed_emails} alert email(s) failed to send")
        return OpsStatusRead(
            status="critical" if critical else "warning" if problems else "ok",
            problems=problems,
            disk_free_mb=disk.free_mb,
            disk_status=disk.status,
            backup=backup,
            permission_findings=perms,
            scheduler=scheduler_state,
            active_sessions=active,
            failed_logins_last_window=failed_logins,
            failed_alert_emails=failed_emails,
        )
