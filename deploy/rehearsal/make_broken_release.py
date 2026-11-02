#!/usr/bin/env python3
"""Turn a good release into a deliberately broken one, to prove automatic rollback.

The broken release (VERSION "broken-<original>"):
  * adds migration 9999_rehearsal_probe, which creates a table — so the database schema
    changes during the deploy and rollback must restore the pre-deploy backup;
  * imports fine (passes the pre-switch import check) but fails at application startup,
    so the service never becomes ready and the deploy must roll itself back.

    python3 make_broken_release.py GOOD.tar.gz OUT.tar.gz
"""

from __future__ import annotations

import io
import re
import sys
import tarfile

MIGRATION = '''"""rehearsal probe (deliberately part of a broken release)"""
import sqlalchemy as sa
from alembic import op

revision = "9999"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("rehearsal_probe", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("rehearsal_probe")
'''


def main(src: str, dest: str) -> None:
    with tarfile.open(src, "r:gz") as tar:
        members = [(m, tar.extractfile(m).read() if m.isfile() else b"") for m in tar.getmembers()]  # type: ignore[union-attr]
    top = members[0][0].name.split("/")[0]
    heads = sorted(
        re.search(rb"^revision(?:: str)? = ['\"](\w+)['\"]", data, re.M).group(1).decode()  # type: ignore[union-attr]
        for m, data in members
        if "/backend/migrations/versions/" in m.name and m.name.endswith(".py")
    )
    out: list[tuple[tarfile.TarInfo, bytes]] = []
    for member, data in members:
        if member.name.endswith("/VERSION"):
            data = b"broken-" + data
        if member.name.endswith("/backend/app/main.py"):
            data = data.replace(
                b"        yield\n",
                b"        raise RuntimeError('rehearsal: deliberately broken release')\n        yield\n",
                1,
            )
        out.append((member, data))
    probe = MIGRATION.format(head=heads[-1]).encode()
    info = tarfile.TarInfo(f"{top}/backend/migrations/versions/9999_rehearsal_probe.py")
    info.size, info.mode = len(probe), 0o644
    out.append((info, probe))
    with tarfile.open(dest, "w:gz") as tar:
        for member, data in out:
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
    print(f"broken release written: {dest} (probe migration down_revision={heads[-1]})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
