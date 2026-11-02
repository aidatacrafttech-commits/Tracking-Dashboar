"""Database lease locks for background jobs (works across processes and restarts)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.time import utcnow
from app.models import JobLock

logger = logging.getLogger(__name__)

# One identity per process: a lock can only be released or renewed by its holder.
PROCESS_ID = f"{uuid.uuid4().hex[:12]}"


class JobLocks:
    def __init__(self, session_factory: sessionmaker[Session], owner: str = PROCESS_ID) -> None:
        self.session_factory = session_factory
        self.owner = owner

    def acquire(self, name: str, ttl: timedelta) -> bool:
        """Atomically take the lock if free or expired. Returns False if someone else holds it."""
        now = utcnow()
        session = self.session_factory()
        try:
            taken = session.execute(
                update(JobLock)
                .where(JobLock.name == name, JobLock.expires_at <= now)
                .values(owner=self.owner, acquired_at=now, expires_at=now + ttl)
            ).rowcount
            if not taken:
                try:
                    session.execute(
                        insert(JobLock).values(
                            name=name, owner=self.owner, acquired_at=now, expires_at=now + ttl
                        )
                    )
                    taken = 1
                except IntegrityError:
                    session.rollback()
                    return False  # held by someone else and not expired
            session.commit()
            return bool(taken)
        finally:
            session.close()

    def release(self, name: str) -> None:
        session = self.session_factory()
        try:
            session.execute(
                delete(JobLock).where(JobLock.name == name, JobLock.owner == self.owner)
            )
            session.commit()
        except Exception:
            logger.exception("failed to release job lock", extra={"lock": name})
        finally:
            session.close()
