"""Server-side sessions, authentication attempts and job lease locks."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base
from app.db.types import UTCDateTime


class AuthSession(Base):
    """A browser session. Only a SHA-256 hash of the cookie value is stored, so a database
    leak does not yield usable sessions. Sessions die on logout, idle timeout, absolute
    expiry, or when the credential that created them is rotated (fingerprint mismatch)."""

    __tablename__ = "auth_sessions"
    __table_args__ = (sa.Index("ix_auth_sessions_expires", "expires_at"),)

    id_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # Short hash of the token used to sign in; rotating that token ends its sessions.
    credential_fingerprint: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    persistent: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    client_ip: Mapped[str | None] = mapped_column(sa.String(64))
    user_agent: Mapped[str | None] = mapped_column(sa.String(200))


class AuthAttempt(Base):
    """Every login attempt and every failed bearer authentication (rate limiting)."""

    __tablename__ = "auth_attempts"
    __table_args__ = (sa.Index("ix_auth_attempts_ip_time", "client_ip", "occurred_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    client_ip: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # login | bearer
    success: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)


class JobLock(Base):
    """Lease lock for background jobs. Acquired with one atomic conditional write, so two
    processes (or a restarted one) can never run the same job at the same time; an
    expired lease is taken over, so a crashed holder cannot block forever."""

    __tablename__ = "job_locks"

    name: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
