"""Disk space, file permissions and SQLite backups."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.config import BACKEND_DIR, Settings
from app.core.time import utcnow

logger = logging.getLogger(__name__)

MB = 1024 * 1024


def sqlite_path(settings: Settings) -> Path | None:
    url = settings.resolved_database_url
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url.endswith(":memory:"):
        return None
    return Path(url[len(prefix) :])


# --- disk ----------------------------------------------------------------------------------


@dataclass
class DiskStatus:
    path: str
    free_mb: int
    status: str  # ok | warning | failed


def disk_status(settings: Settings) -> DiskStatus:
    db = sqlite_path(settings)
    target = db.parent if db else BACKEND_DIR
    probe = target if target.exists() else BACKEND_DIR
    free = shutil.disk_usage(probe).free // MB
    limit = settings.disk_min_free_mb
    status = "failed" if free < limit else "warning" if free < limit * 2 else "ok"
    return DiskStatus(path=probe.name, free_mb=free, status=status)


# --- permissions ------------------------------------------------------------------------------

BROAD_PRINCIPALS = ("Everyone:", "BUILTIN\\Users:", "NT AUTHORITY\\Authenticated Users:", "Users:")


@dataclass
class PermissionFinding:
    path: str
    problem: str


def _windows_broad_access(path: Path) -> str | None:
    # Absolute path to the system icacls (never resolved through PATH).
    icacls = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe"
    try:
        out = subprocess.run(  # noqa: S603 - fixed system binary, path argument only
            [str(icacls), str(path)], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        for principal in BROAD_PRINCIPALS:
            if principal in line:
                return f"accessible to {principal.rstrip(':')}"
    return None


def permission_findings(settings: Settings) -> list[PermissionFinding]:
    """Secrets and data should be readable only by the service account."""
    candidates = [BACKEND_DIR / ".env", settings.backup_dir]
    db = sqlite_path(settings)
    if db:
        candidates += [db, db.parent]
    findings: list[PermissionFinding] = []
    for path in candidates:
        if not path.exists():
            continue
        label = path.name or str(path)
        if sys.platform == "win32":
            problem = _windows_broad_access(path)
            if problem:
                findings.append(PermissionFinding(label, problem))
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                findings.append(
                    PermissionFinding(label, f"mode {oct(mode)} allows group/other access")
                )
    return findings


def restrict(path: Path) -> None:
    """Best-effort owner-only permissions on POSIX (Windows: use scripts/harden_permissions.ps1)."""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            logger.warning("could not restrict permissions", extra={"path": path.name})


# --- backups ------------------------------------------------------------------------------------


class BackupError(Exception):
    pass


@dataclass
class BackupResult:
    file: str
    size_bytes: int
    finished_at: datetime
    pruned: list[str] = field(default_factory=list)


def _without_runtime_state(conn: sqlite3.Connection, run_id: int | None) -> None:
    """The copy is taken while this backup run (and often the alert cycle) holds a lease.
    Neither is meaningful in a restored database: drop the leases, and record this backup's
    own run as succeeded (the copy is only kept if it passes the integrity check)."""
    try:
        conn.execute("DELETE FROM job_locks")
        if run_id is not None:
            conn.execute(
                "UPDATE monitoring_runs SET status = 'succeeded', finished_at = started_at,"
                " records_written = 1 WHERE id = ?",
                (run_id,),
            )
        conn.commit()
    except sqlite3.OperationalError:  # schema older than these tables
        conn.rollback()


def run_backup(settings: Settings, run_id: int | None = None) -> BackupResult:
    """Consistent online copy via SQLite's backup API, verified before it is kept."""
    source = sqlite_path(settings)
    if source is None or not source.exists():
        raise BackupError("backups are supported for file-based SQLite only")
    target_dir = settings.backup_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    restrict(target_dir)

    needed = source.stat().st_size * 2 + settings.disk_min_free_mb * MB
    free = shutil.disk_usage(target_dir).free
    if free < needed:
        raise BackupError(f"insufficient disk space for backup ({free // MB} MB free)")

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    final = target_dir / f"monitor-{stamp}.db"
    partial = final.with_suffix(".db.partial")
    try:
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            _without_runtime_state(dst, run_id)
        finally:
            dst.close()
            src.close()
        check = sqlite3.connect(partial)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise BackupError("backup failed its integrity check")
        partial.replace(final)
        restrict(final)
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    except (sqlite3.Error, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"backup failed ({type(exc).__name__})") from exc

    backups = sorted(target_dir.glob("monitor-*.db"), reverse=True)
    pruned = []
    for old in backups[settings.backup_keep :]:
        old.unlink(missing_ok=True)
        pruned.append(old.name)
    return BackupResult(
        file=final.name, size_bytes=final.stat().st_size, finished_at=utcnow(), pruned=pruned
    )
