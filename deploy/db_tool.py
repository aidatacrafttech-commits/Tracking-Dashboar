#!/usr/bin/env python3
"""SQLite helpers for deploy, restore and rollback. Standard library only.

    db_tool.py backup  SRC DEST   online, consistent copy (safe while the service runs),
                                  integrity-checked before it is kept
    db_tool.py check   FILE       integrity check; prints the schema revision
    db_tool.py revision FILE      prints only the schema revision (alembic_version)
    db_tool.py clear-locks FILE   drop job leases (only with the service stopped)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def _open(path: Path) -> sqlite3.Connection:
    """Open a file that is NOT in use (a backup, or the database with the service stopped).
    Only reads are run; a normal connection lets SQLite remove the -wal/-shm files it needs
    for WAL-mode files on close, which a read-only connection would leave behind."""
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path)


def _integrity(path: Path) -> str:
    con = _open(path)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def _revision(path: Path) -> str:
    if not path.is_file():
        return "none"
    con = _open(path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else "none"
    except sqlite3.OperationalError:
        return "none"
    finally:
        con.close()


def _clear_locks(con: sqlite3.Connection) -> int:
    """Job leases are runtime state of the process that took them; in a copy or a restored
    file (service stopped) they only block scheduled work until they expire."""
    try:
        removed = con.execute("DELETE FROM job_locks").rowcount
        con.commit()
        return removed
    except sqlite3.OperationalError:  # schema older than the job_locks table
        return 0


def clear_locks(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        print(f"cleared {_clear_locks(con)} stale job lock(s) in {path.name}")
        return 0
    finally:
        con.close()


def backup(src: Path, dest: Path) -> int:
    if not src.exists():
        print(f"no database at {src}; nothing to back up")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    old_umask = os.umask(0o077)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
            _clear_locks(target)
            # A backup is one self-contained file: no -wal/-shm companions.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()
    finally:
        os.umask(old_umask)
    result = _integrity(partial)
    if result != "ok":
        partial.unlink(missing_ok=True)
        print(f"backup failed integrity check: {result}", file=sys.stderr)
        return 1
    partial.replace(dest)
    print(f"backup ok: {dest} (revision {_revision(dest)}, {dest.stat().st_size} bytes)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "backup":
        return backup(Path(argv[1]), Path(argv[2]))
    if len(argv) == 2 and argv[0] == "check":
        path = Path(argv[1])
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            return 1
        result = _integrity(path)
        print(f"integrity: {result}; revision: {_revision(path)}")
        return 0 if result == "ok" else 1
    if len(argv) == 2 and argv[0] == "clear-locks":
        return clear_locks(Path(argv[1]))
    if len(argv) == 2 and argv[0] == "revision":
        print(_revision(Path(argv[1])))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
