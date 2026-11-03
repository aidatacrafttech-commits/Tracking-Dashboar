"""Live presence: which app users have the AdhyaapakAI app open right now.

The mobile app sends `POST /api/v1/integrations/presence/heartbeat` every 30 seconds while it
is in the foreground, and `{"state": "background"}` when it goes to the background or closes.
A user counts as online until two heartbeats are missed (`PRESENCE_TTL`), so a crashed or
killed app drops out within about a minute, and a clean close drops out at once.

The app authenticates with its own login token (an HS256 JWT signed with the AdhyaapakAI
backend's JWT_SECRET, configured here as ADHYAAPAK_JWT_SECRET). It is verified locally, so
the AdhyaapakAI backend is never called. Presence lives in this process's memory only (the
dashboard runs one worker): a restart empties it, and the next heartbeats refill it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading
import time
from datetime import datetime, timedelta

from app.core.errors import AuthenticationError

PRESENCE_TTL = timedelta(seconds=75)  # heartbeats every 30 s: two missed = closed
MAX_TRACKED = 200_000  # a safety cap far above the app's user count
_USER_ID = re.compile(r"^[0-9a-f]{24}$")


class PresenceTracker:
    """Thread-safe map of user id -> last heartbeat (monotonic seconds)."""

    def __init__(self, ttl: timedelta = PRESENCE_TTL) -> None:
        self._ttl = ttl.total_seconds()
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def beat(self, user_id: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._seen) >= MAX_TRACKED and user_id not in self._seen:
                self._prune(now)
                if len(self._seen) >= MAX_TRACKED:
                    return
            self._seen[user_id] = now

    def leave(self, user_id: str) -> None:
        with self._lock:
            self._seen.pop(user_id, None)

    def online(self, now: float | None = None) -> int:
        return len(self.online_ids(now))

    def online_ids(self, now: float | None = None) -> set[str]:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune(now)
            return set(self._seen)

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        self._seen = {uid: at for uid, at in self._seen.items() if at >= cutoff}


def _b64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_app_token(token: str, secret: str, now: datetime) -> str:
    """The user id from an AdhyaapakAI login token, or AuthenticationError. Only HS256 is
    accepted, the signature is compared in constant time, and `exp` must be in the future."""
    invalid = AuthenticationError("Invalid or expired app token", code="invalid_app_token")
    parts = token.split(".")
    if len(parts) != 3 or len(token) > 4096:
        raise invalid
    try:
        header = json.loads(_b64url(parts[0]))
        signature = _b64url(parts[2])
        payload = json.loads(_b64url(parts[1]))
    except (ValueError, binascii.Error) as exc:
        raise invalid from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise invalid
    expected = hmac.new(
        secret.encode(), f"{parts[0]}.{parts[1]}".encode("ascii", "replace"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise invalid
    if not isinstance(payload, dict):
        raise invalid
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or exp <= now.timestamp():
        raise invalid
    sub = payload.get("sub")
    if not isinstance(sub, str) or not _USER_ID.match(sub):
        raise invalid
    return sub
