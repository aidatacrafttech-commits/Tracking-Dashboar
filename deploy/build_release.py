#!/usr/bin/env python3
"""Build a deployable release tarball. Run on the development machine:

    cd frontend && npm run build && cd ..
    backend\\venv\\Scripts\\python deploy\\build_release.py        (Windows)
    backend/venv/bin/python deploy/build_release.py                (Linux/macOS)

Output: release/api-cost-monitor-<version>.tar.gz (+ .sha256). The archive contains the
backend code, migrations, the pinned dependency lock, the built UI, the deploy kit and
the docs — never .env files, databases, backups, virtualenvs, tests or caches. The build
aborts if anything that looks like a credential is about to be packaged.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
INCLUDE = [
    ("backend/app", "**/*.py"),
    ("backend/migrations", "**/*"),
    ("backend", "alembic.ini"),
    ("backend", "pyproject.toml"),
    ("backend", "requirements.txt"),
    ("backend", "requirements.lock"),
    ("frontend/dist", "**/*"),
    ("deploy", "*.py"),
    ("deploy", "*.sh"),
    ("deploy", "monitor.env.example"),
    ("deploy/systemd", "*"),
    ("deploy/nginx", "*"),
    ("docs", "*.md"),
    ("docs", "aws-monitoring-iam-policy.json"),
]
FORBIDDEN_NAME = re.compile(r"(^\.env($|\.)(?!example$)|\.db$|\.db-|\.sqlite|\.pem$|\.key$|id_rsa|\.p12$)")
SECRET_PATTERNS = [
    re.compile(rb"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-(proj-|admin-)?[A-Za-z0-9_-]{24,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
LF_SUFFIXES = {".sh", ".service", ".timer", ".conf", ".example", ".py", ".ini", ".txt", ".lock", ".toml", ".md", ".json", ".mako"}
EXECUTABLE = {"deploy/manage.sh", "deploy/inspect_host.sh", "deploy/smoke_test.py", "deploy/db_tool.py", "deploy/check_outbound.py"}


def collect() -> list[Path]:
    files: set[Path] = set()
    for base, pattern in INCLUDE:
        directory = ROOT / base
        if not directory.exists():
            sys.exit(f"missing {base}/ — build the frontend first (npm run build)" if "dist" in base else f"missing {base}")
        files.update(p for p in directory.glob(pattern) if p.is_file())
    return sorted(
        p for p in files
        if "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    )


def main() -> int:
    if not (ROOT / "backend/requirements.lock").exists():
        sys.exit("backend/requirements.lock is missing (see docs/DEPLOYMENT.md, 'Dependency lock')")
    dist_index = ROOT / "frontend/dist/index.html"
    newest_src = max(p.stat().st_mtime for p in (ROOT / "frontend/src").rglob("*") if p.is_file())
    if not dist_index.exists() or dist_index.stat().st_mtime < newest_src:
        sys.exit("frontend/dist is missing or older than frontend/src — run `npm run build` first")

    files = collect()
    payload: list[tuple[str, bytes]] = []
    digest = hashlib.sha256()
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if FORBIDDEN_NAME.search(path.name):
            sys.exit(f"refusing to package {rel}")
        data = path.read_bytes()
        if path.suffix in LF_SUFFIXES or path.name.endswith(".example"):
            data = data.replace(b"\r\n", b"\n")
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                sys.exit(f"refusing to package {rel}: it contains something that looks like a credential")
        payload.append((rel, data))
        digest.update(rel.encode() + b"\0" + data)

    version = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{digest.hexdigest()[:8]}"
    payload.append(("VERSION", f"{version}\n".encode()))
    OUT.mkdir(exist_ok=True)
    target = OUT / f"api-cost-monitor-{version}.tar.gz"
    top = f"api-cost-monitor-{version}"
    now = int(time.time())
    with tarfile.open(target, "w:gz") as tar:
        for rel, data in payload:
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            info.mtime = now
            info.mode = 0o755 if rel in EXECUTABLE else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    (OUT / f"{target.name}.sha256").write_text(f"{sha}  {target.name}\n", encoding="utf-8")
    print(f"release: {target.relative_to(ROOT).as_posix()}")
    print(f"version: {version}")
    print(f"files:   {len(payload)}")
    print(f"sha256:  {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
