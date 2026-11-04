"""App Users: read-only analytics over the AdhyaapakAI MongoDB.

The dashboard SERVER reads the app's production database. The browser gets counts, and per-day
signup lists: full name, email and phone for the owner, masked for viewers. Safeguards:
- `MongoSource` exposes a handful of fixed queries built from `find` and `count_documents`
  on three allow-listed collections, with tight projections and a server-side time limit.
  There is no write call anywhere in this module.
- Results are cached for a minute so a busy dashboard does not load production.
- Logs carry the error type only: never documents, emails or the connection URL.

"New signups" are completed registrations (`users` documents). The app records no install or
first-open event, so real install counts only exist in Play Console / App Store Connect.
Days are calendar days in the dashboard's display timezone (the app itself uses IST).
"""

from __future__ import annotations

import logging
import threading
import time as monotonic
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.errors import (
    ConfigurationError,
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from app.core.config import Settings
from app.core.errors import AppError, DomainValidationError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# The App Store review account (created by the app itself) is not a real user.
REVIEWER_EMAIL = "applereviewer@adhyaapak.ai"
# `users.last_active_at` is only written since this date and is overwritten on each visit.
ACTIVE_TRACKED_SINCE = date(2026, 10, 28)
MAX_TIME_MS = 10_000
CACHE_SECONDS = 60
LIST_LIMIT = 2000  # rows in one signup list (the app gets a few a day)
MAX_RANGE_DAYS = 90
# "Online now" without any app change: every signal the app already writes, each with a window
# just longer than its write interval, so a user who is using the app is never missed.
# users.last_active_at: any authenticated request, written at most every 10 minutes per user.
ONLINE_WINDOW = timedelta(minutes=12)
# The player saves to recentWatching every 5-10 s while a video plays (it skips a save when
# under 5 s of progress was made), so 25 s never misses a viewer.
VIDEO_WINDOW = timedelta(seconds=25)
LIVE_TUTOR_WINDOW = timedelta(seconds=30)  # Live Tutor heartbeats every 10-15 s
DOUBTS_WINDOW = timedelta(minutes=5)  # every doubt message updates its session
TESTS_WINDOW = timedelta(minutes=5)  # a finished test
LIVE_CACHE_SECONDS = 3  # one database read per 3 s, however many dashboards are open
ROW_CAP = 200_000  # a safety stop for one query, far above the app's daily volume
NOT_SET = "Not set"
T = TypeVar("T")


class AppUsersError(AppError):
    """A MongoDB failure reported to the dashboard (no driver detail is echoed)."""

    def __init__(self, status_code: int, message: str, *, code: str) -> None:
        super().__init__(message, code=code)
        self.status_code = status_code


@dataclass(frozen=True)
class SignupRow:
    created: datetime  # aware UTC (the user's ObjectId time)
    class_level: int | None = None
    stream: str | None = None
    goals: tuple[str, ...] = ()
    has_course: bool = False


@dataclass(frozen=True)
class PurchaseRow:
    created: datetime  # aware UTC
    store: str | None
    amount_inr: Any  # number as stored (rupees); App Store rows may be another currency
    currency: str | None


@dataclass(frozen=True)
class RecentSignup:
    created: datetime
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    class_level: int | None = None
    stream: str | None = None
    goals: tuple[str, ...] = ()
    has_course: bool = False
    deletion_scheduled: bool = False


@dataclass(frozen=True)
class DeletionRow:
    """One entry of the app's deletion journal, plus the profile while it still exists."""

    user_id: str
    status: str | None  # scheduled, reactivated, in_progress or completed
    requested_at: datetime | None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    reactivated_at: datetime | None = None
    signed_up_at: datetime | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    profile_exists: bool = False


class AppUsersSource(Protocol):
    """The only questions this module asks the app database."""

    def signups(self, start: datetime, end: datetime) -> list[SignupRow]: ...
    def purged_signups(self, start: datetime, end: datetime) -> list[datetime]: ...
    def purchases(self, start: datetime, end: datetime) -> list[PurchaseRow]: ...
    def active_since(self, since: datetime) -> int: ...
    def scheduled_deletions(self) -> int: ...
    def total_users(self) -> int: ...
    def signup_details(self, start: datetime, end: datetime, limit: int) -> list[RecentSignup]: ...
    def deletions(self, start: datetime, end: datetime, limit: int) -> list[DeletionRow]: ...
    def live_user_ids(self, now: datetime) -> dict[str, set[str]]: ...
    def close(self) -> None: ...


# --- Helpers (pure) --------------------------------------------------------------------


def day_start(day: date, tz: ZoneInfo) -> datetime:
    """The UTC instant at which `day` begins in `tz`."""
    return datetime.combine(day, time(), tz).astimezone(UTC)


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def period(days: int, tz: ZoneInfo, now: datetime) -> list[date]:
    """The last `days` calendar days in `tz`, oldest first, ending today."""
    today = local_day(now, tz)
    return [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def resolve_range(
    tz: ZoneInfo, now: datetime, days: int | None, start: date | None, end: date | None
) -> tuple[date, date]:
    """The selected calendar days (inclusive): either the last `days` or start..end."""
    today = local_day(now, tz)
    if start is None and end is None:
        n = days or 30
        return today - timedelta(days=n - 1), today
    if start is None or end is None:
        raise DomainValidationError("Choose both a start and an end date.", code="range_incomplete")
    if start > end:
        raise DomainValidationError("The start date is after the end date.", code="range_reversed")
    if end > today:
        raise DomainValidationError("The end date is in the future.", code="range_in_future")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise DomainValidationError(f"Choose at most {MAX_RANGE_DAYS} days.", code="range_too_long")
    return start, end


def mask_email(email: str) -> str:
    """Enough to recognise, not the full address."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def mask_phone(phone: str | None) -> str | None:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return None
    return "******" + digits[-4:] if len(digits) >= 6 else "***"


def first_name(name: str | None) -> str | None:
    parts = str(name or "").split()
    return parts[0][:40] if parts else None


def to_paise(amount: Any) -> int:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return 0
    if not value.is_finite() or value <= 0:
        return 0
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _class_label(value: int | None) -> str:
    return f"Class {value}" if value is not None else NOT_SET


def _counted(values: Iterable[str]) -> list[dict[str, Any]]:
    counts = Counter(values)
    return [
        {"label": label, "count": n}
        for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _is_inr(row: PurchaseRow) -> bool:
    # Razorpay rows carry no currency (always INR); App Store rows may be another currency.
    return row.currency is None or str(row.currency).upper() == "INR"


# --- MongoDB (read-only) ------------------------------------------------------------------


class MongoSource:
    """Fixed read-only queries. Only `find` and `count_documents` are ever called."""

    COLLECTIONS = frozenset(
        {
            "users",
            "account_deletions",
            "entitlement_purchases",
            "ai_tutor_usage_sessions",
            "video_progress",
            "recentWatching",
            "doubt_sessions",
            "test_attempts",
        }
    )

    def __init__(self, url: str, database: str, client: Any | None = None) -> None:
        if client is None:
            client = MongoClient(
                url,
                appname="api-cost-monitor",
                tz_aware=True,
                connect=False,  # nothing is opened until the screen is first used
                maxPoolSize=2,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=15000,
                readPreference="secondaryPreferred",
                retryWrites=False,
            )
        self._client = client
        self._db = client[database]

    def close(self) -> None:
        self._client.close()

    def _coll(self, name: str) -> Any:
        if name not in self.COLLECTIONS:
            raise ValueError(f"collection {name!r} is not readable from the dashboard")
        return self._db[name]

    def _find(
        self,
        name: str,
        flt: dict[str, Any],
        projection: dict[str, int],
        *,
        sort: list[tuple[str, int]] | None = None,
        limit: int = ROW_CAP,
    ) -> Iterable[dict[str, Any]]:
        cursor = self._coll(name).find(flt, projection, max_time_ms=MAX_TIME_MS, limit=limit)
        return cursor.sort(sort) if sort else cursor

    def _count(self, name: str, flt: dict[str, Any]) -> int:
        return int(self._coll(name).count_documents(flt, maxTimeMS=MAX_TIME_MS))

    @staticmethod
    def _oid(moment: datetime) -> ObjectId:
        return ObjectId.from_datetime(moment)

    @staticmethod
    def _created(doc: dict[str, Any]) -> datetime:
        return doc["_id"].generation_time  # type: ignore[no-any-return]

    def _id_range(self, start: datetime, end: datetime) -> dict[str, Any]:
        # `created_at` has no index; the ObjectId carries the same instant and is indexed.
        return {"$gte": self._oid(start), "$lt": self._oid(end)}

    def signups(self, start: datetime, end: datetime) -> list[SignupRow]:
        docs = self._find(
            "users",
            {"_id": self._id_range(start, end), "email": {"$ne": REVIEWER_EMAIL}},
            {"_id": 1, "class": 1, "stream": 1, "goals": 1, "entitlements": 1},
        )
        return [
            SignupRow(
                created=self._created(d),
                class_level=_int_or_none(d.get("class")),
                stream=_str_or_none(d.get("stream")),
                goals=_goals(d.get("goals")),
                has_course=bool(d.get("entitlements")),
            )
            for d in docs
        ]

    def purged_signups(self, start: datetime, end: datetime) -> list[datetime]:
        # The deletion journal is keyed by the user's id as a hex string: hex ObjectIds of
        # equal length sort like the ObjectIds themselves, so a string range works.
        flt = {
            "_id": {"$gte": str(self._oid(start)), "$lt": str(self._oid(end)), "$type": "string"},
            "status": "completed",
        }
        out: list[datetime] = []
        for doc in self._find("account_deletions", flt, {"_id": 1}):
            try:
                out.append(ObjectId(doc["_id"]).generation_time)
            except (InvalidId, TypeError):
                continue
        return out

    def purchases(self, start: datetime, end: datetime) -> list[PurchaseRow]:
        docs = self._find(
            "entitlement_purchases",
            {"created_at": {"$gte": start, "$lt": end}, "refunded": {"$ne": True}},
            {"_id": 0, "created_at": 1, "store": 1, "amount_inr": 1, "currency": 1},
        )
        return [
            PurchaseRow(
                created=_aware(d["created_at"]),
                store=_str_or_none(d.get("store")),
                amount_inr=d.get("amount_inr"),
                currency=_str_or_none(d.get("currency")),
            )
            for d in docs
            if isinstance(d.get("created_at"), datetime)
        ]

    def active_since(self, since: datetime) -> int:
        return self._count(
            "users", {"last_active_at": {"$gte": since}, "email": {"$ne": REVIEWER_EMAIL}}
        )

    def scheduled_deletions(self) -> int:
        return self._count("account_deletions", {"status": "scheduled"})

    def total_users(self) -> int:
        return self._count("users", {"email": {"$ne": REVIEWER_EMAIL}})

    def signup_details(self, start: datetime, end: datetime, limit: int) -> list[RecentSignup]:
        docs = self._find(
            "users",
            {"_id": self._id_range(start, end), "email": {"$ne": REVIEWER_EMAIL}},
            {
                "_id": 1,
                "name": 1,
                "email": 1,
                "phone": 1,
                "class": 1,
                "stream": 1,
                "goals": 1,
                "entitlements": 1,
                "deletion_status": 1,
            },
            sort=[("_id", -1)],
            limit=limit,
        )
        return [
            RecentSignup(
                created=self._created(d),
                name=_str_or_none(d.get("name")),
                email=_str_or_none(d.get("email"), 254),
                phone=_str_or_none(d.get("phone"), 20),
                class_level=_int_or_none(d.get("class")),
                stream=_str_or_none(d.get("stream")),
                goals=_goals(d.get("goals")),
                has_course=bool(d.get("entitlements")),
                deletion_scheduled=d.get("deletion_status") in ("scheduled", "pending"),
            )
            for d in docs
        ]

    def deletions(self, start: datetime, end: datetime, limit: int) -> list[DeletionRow]:
        # A deletion is dated by its request (an immediate purge has no request: its start).
        window = {"$gte": start, "$lt": end}
        journal = list(
            self._find(
                "account_deletions",
                {
                    "$or": [
                        {"requested_at": window},
                        {"requested_at": {"$exists": False}, "started_at": window},
                    ]
                },
                {
                    "_id": 1,
                    "status": 1,
                    "requested_at": 1,
                    "started_at": 1,
                    "due_at": 1,
                    "completed_at": 1,
                    "reactivated_at": 1,
                },
                limit=limit,
            )
        )
        ids = {}
        for j in journal:
            try:
                ids[str(j["_id"])] = ObjectId(j["_id"])
            except (InvalidId, TypeError):
                continue
        # Profiles still exist for scheduled and restored accounts; a purge removes them.
        profiles = (
            {
                str(d["_id"]): d
                for d in self._find(
                    "users",
                    {"_id": {"$in": list(ids.values())}},
                    {"_id": 1, "name": 1, "email": 1, "phone": 1},
                    limit=limit,
                )
            }
            if ids
            else {}
        )
        out: list[DeletionRow] = []
        for j in journal:
            uid = str(j["_id"])
            user = profiles.get(uid)
            requested = j.get("requested_at") or j.get("started_at")
            out.append(
                DeletionRow(
                    user_id=uid,
                    status=_str_or_none(j.get("status")),
                    requested_at=_aware(requested) if isinstance(requested, datetime) else None,
                    due_at=_maybe_aware(j.get("due_at")),
                    completed_at=_maybe_aware(j.get("completed_at")),
                    reactivated_at=_maybe_aware(j.get("reactivated_at")),
                    signed_up_at=ids[uid].generation_time if uid in ids else None,
                    name=_str_or_none(user.get("name")) if user else None,
                    email=_str_or_none(user.get("email"), 254) if user else None,
                    phone=_str_or_none(user.get("phone"), 20) if user else None,
                    profile_exists=user is not None,
                )
            )
        return out

    def live_user_ids(self, now: datetime) -> dict[str, set[str]]:
        """User ids per live signal. Only the id field is read (doubt sessions hold messages
        and images, which are never fetched)."""

        def ids(name: str, flt: dict[str, Any], key: str) -> set[str]:
            projection = {"_id": 1} if key == "_id" else {"_id": 0, key: 1}
            return {str(d[key]) for d in self._find(name, flt, projection) if d.get(key)}

        def since(window: timedelta) -> dict[str, datetime]:
            return {"$gte": now - window}

        return {
            "app": ids(
                "users",
                {"last_active_at": since(ONLINE_WINDOW), "email": {"$ne": REVIEWER_EMAIL}},
                "_id",
            ),
            "video": ids("video_progress", {"last_watched_at": since(VIDEO_WINDOW)}, "user_id")
            | ids("recentWatching", {"lastWatchedAt": since(VIDEO_WINDOW)}, "userId"),
            "tutor": ids(
                "ai_tutor_usage_sessions",
                {"status": "active", "last_tick_at": since(LIVE_TUTOR_WINDOW)},
                "user_id",
            ),
            "doubts": ids("doubt_sessions", {"updated_at": since(DOUBTS_WINDOW)}, "user_id"),
            "tests": ids("test_attempts", {"completed_at": since(TESTS_WINDOW)}, "user_id"),
        }


def _maybe_aware(value: Any) -> datetime | None:
    return _aware(value) if isinstance(value, datetime) else None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any, limit: int = 80) -> str | None:
    return (value.strip()[:limit] or None) if isinstance(value, str) else None


def _goals(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(g.strip()[:40] for g in value if isinstance(g, str) and g.strip())[:10]


def build_app_users_source(settings: Settings) -> AppUsersSource | None:
    """None when not configured. The client connects lazily, so startup never waits on it."""
    if settings.adhyaapak_mongo_url is None or not settings.adhyaapak_mongo_db:
        return None
    try:
        return MongoSource(
            settings.adhyaapak_mongo_url.get_secret_value(), settings.adhyaapak_mongo_db
        )
    except Exception as exc:  # pragma: no cover - misconfiguration must not stop startup
        logger.warning(
            "app users source could not be initialised", extra={"error_type": type(exc).__name__}
        )
        return None


# --- Cache ---------------------------------------------------------------------------------


@dataclass
class TtlCache:
    seconds: float = CACHE_SECONDS
    _items: dict[tuple[Any, ...], tuple[float, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get_or_set(self, key: tuple[Any, ...], compute: Callable[[], T]) -> T:
        now = monotonic.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]  # type: ignore[no-any-return]
        value = compute()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            self._items[key] = (now + self.seconds, value)
        return value


# --- Service -------------------------------------------------------------------------------


class AppUsersService:
    def __init__(self, source: AppUsersSource | None, cache: TtlCache) -> None:
        if source is None:
            raise ServiceUnavailableError(
                "App users are not configured: set ADHYAAPAK_MONGO_URL and ADHYAAPAK_MONGO_DB "
                "in the dashboard's backend/.env and restart it",
                code="app_users_not_configured",
            )
        self._source = source
        self._cache = cache

    def _call(self, what: str, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except (ServerSelectionTimeoutError, ExecutionTimeout, NetworkTimeout) as exc:
            logger.warning(
                "app users query timed out", extra={"query": what, "error_type": type(exc).__name__}
            )
            raise AppUsersError(
                504, "The app database did not answer in time. Try again.", code="app_users_timeout"
            ) from exc
        except OperationFailure as exc:
            logger.warning(
                "app users query refused",
                extra={"query": what, "error_type": type(exc).__name__, "mongo_code": exc.code},
            )
            if exc.code in (13, 18):  # Unauthorized / AuthenticationFailed
                raise AppUsersError(
                    502,
                    "The app database refused the dashboard's login (ADHYAAPAK_MONGO_URL).",
                    code="app_users_forbidden",
                ) from exc
            raise AppUsersError(
                502, "The app database rejected the query.", code="app_users_query_failed"
            ) from exc
        except (ConfigurationError, PyMongoError) as exc:
            logger.warning(
                "app users query failed", extra={"query": what, "error_type": type(exc).__name__}
            )
            raise AppUsersError(
                502, "The app database could not be reached.", code="app_users_unreachable"
            ) from exc

    def summary(self, first: date, last: date, tz_name: str, now: datetime) -> dict[str, Any]:
        tz = ZoneInfo(tz_name)
        key = ("summary", first, last, tz_name, local_day(now, tz))
        return self._cache.get_or_set(key, lambda: self._summary(first, last, tz, tz_name, now))

    def _summary(
        self, first: date, last: date, tz: ZoneInfo, tz_name: str, now: datetime
    ) -> dict[str, Any]:
        today = local_day(now, tz)
        yesterday = today - timedelta(days=1)
        # One read covers the selected days and the "Today" block (today and yesterday).
        lo, hi = min(first, yesterday), max(last, today)
        start, end = day_start(lo, tz), day_start(hi + timedelta(days=1), tz)

        signups = self._call("signups", lambda: self._source.signups(start, end))
        purged = self._call("purged_signups", lambda: self._source.purged_signups(start, end))
        purchases = self._call("purchases", lambda: self._source.purchases(start, end))
        active_today = self._call("active", lambda: self._source.active_since(day_start(today, tz)))
        active_7d = self._call("active", lambda: self._source.active_since(now - timedelta(days=7)))
        active_30d = self._call(
            "active", lambda: self._source.active_since(now - timedelta(days=30))
        )
        scheduled = self._call("scheduled_deletions", self._source.scheduled_deletions)
        total = self._call("total_users", self._source.total_users)

        all_days = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
        daily: dict[date, dict[str, Any]] = {
            d: {
                "date": d,
                "signups": 0,
                "deleted_since": 0,
                "purchases": 0,
                "vouchers": 0,
                "revenue_paise": 0,
                "other_currency": 0,
            }
            for d in all_days
        }
        for s in signups:
            if (row := daily.get(local_day(s.created, tz))) is not None:
                row["signups"] += 1
        for moment in purged:
            if (row := daily.get(local_day(moment, tz))) is not None:
                row["deleted_since"] += 1
        for p in purchases:
            if (row := daily.get(local_day(p.created, tz))) is None:
                continue
            if p.store == "voucher":
                row["vouchers"] += 1
                continue
            row["purchases"] += 1
            if _is_inr(p):
                row["revenue_paise"] += to_paise(p.amount_inr)
            else:
                row["other_currency"] += 1

        rows = [daily[d] for d in all_days if first <= d <= last]
        in_range = [s for s in signups if first <= local_day(s.created, tz) <= last]
        joined = [r["signups"] + r["deleted_since"] for r in rows]
        best = max(range(len(rows)), key=lambda i: (joined[i], rows[i]["date"]))
        period_signups = sum(joined)
        t, y = daily[today], daily[yesterday]
        return {
            "timezone": tz_name,
            "start": first,
            "end": last,
            "days": len(rows),
            "generated_at": now,
            "active_tracked_since": ACTIVE_TRACKED_SINCE,
            "today": {
                "date": today,
                "signups": t["signups"] + t["deleted_since"],
                "signups_yesterday": y["signups"] + y["deleted_since"],
                "active_today": active_today,
                "active_7d": active_7d,
                "active_30d": active_30d,
                "purchases": t["purchases"],
                "vouchers": t["vouchers"],
                "revenue_paise": t["revenue_paise"],
                "deletions_scheduled": scheduled,
            },
            "totals": {
                "signups": period_signups,
                "deleted_since": sum(r["deleted_since"] for r in rows),
                "average_per_day": round(period_signups / len(rows), 1),
                "best_day": rows[best]["date"] if joined[best] else None,
                "best_day_signups": joined[best],
                "with_course": sum(1 for s in in_range if s.has_course),
                "purchases": sum(r["purchases"] for r in rows),
                "vouchers": sum(r["vouchers"] for r in rows),
                "revenue_paise": sum(r["revenue_paise"] for r in rows),
                "other_currency_purchases": sum(r["other_currency"] for r in rows),
                "total_users": total,
            },
            "daily": [{k: v for k, v in r.items() if k != "other_currency"} for r in rows],
            # Breakdowns cover accounts that still exist (purged ones have no profile left).
            "by_class": _counted(_class_label(s.class_level) for s in in_range),
            "by_stream": _counted(s.stream or NOT_SET for s in in_range),
            "by_goal": _counted(g for s in in_range for g in s.goals),
        }

    def signups_between(
        self, first: date, last: date, tz_name: str, now: datetime, *, full: bool
    ) -> dict[str, Any]:
        """Signups from `first` to `last` (inclusive, already validated), newest first. `full`
        (owner only) shows the full name, email and phone; otherwise they are masked here and
        the full values never leave the server."""
        tz = ZoneInfo(tz_name)
        start, end = day_start(first, tz), day_start(last + timedelta(days=1), tz)
        rows = self._cache.get_or_set(
            ("signups_between", first, last, tz_name, local_day(now, tz)),
            lambda: self._call(
                "signup_details", lambda: self._source.signup_details(start, end, LIST_LIMIT + 1)
            ),
        )
        shown = rows[:LIST_LIMIT]
        return {
            "start": first,
            "end": last,
            "timezone": tz_name,
            "masked": not full,
            "more": len(rows) > LIST_LIMIT,
            "signups": [
                {
                    "signed_up_at": r.created,
                    "name": r.name if full else first_name(r.name),
                    "class_level": r.class_level,
                    "stream": r.stream,
                    "goals": list(r.goals),
                    "email": r.email if full else (mask_email(r.email) if r.email else None),
                    "phone": r.phone if full else mask_phone(r.phone),
                    "has_course": r.has_course,
                    "deletion_scheduled": r.deletion_scheduled,
                }
                for r in shown
            ],
        }

    def deletions_between(
        self, first: date, last: date, tz_name: str, now: datetime, *, full: bool
    ) -> dict[str, Any]:
        """Deletion requests made from `first` to `last` (inclusive), newest first, with the
        profile while it still exists. A completed deletion has no profile left: the app
        removed it, and nothing here tries to rebuild it. Masked for viewers like signups."""
        tz = ZoneInfo(tz_name)
        start, end = day_start(first, tz), day_start(last + timedelta(days=1), tz)
        rows = self._cache.get_or_set(
            ("deletions_between", first, last, tz_name, local_day(now, tz)),
            lambda: self._call(
                "deletions", lambda: self._source.deletions(start, end, LIST_LIMIT + 1)
            ),
        )
        ordered = sorted(rows, key=lambda r: r.requested_at or start, reverse=True)
        shown = ordered[:LIST_LIMIT]
        return {
            "start": first,
            "end": last,
            "timezone": tz_name,
            "masked": not full,
            "more": len(rows) > LIST_LIMIT,
            "deletions": [
                {
                    "requested_at": r.requested_at,
                    "status": r.status,
                    "due_at": r.due_at,
                    "completed_at": r.completed_at,
                    "reactivated_at": r.reactivated_at,
                    "signed_up_at": r.signed_up_at,
                    "profile_exists": r.profile_exists,
                    "name": r.name if full else first_name(r.name),
                    "email": r.email if full else (mask_email(r.email) if r.email else None),
                    "phone": r.phone if full else mask_phone(r.phone),
                }
                for r in shown
            ],
        }

    def live(self, now: datetime, *, app_open: set[str] | None) -> dict[str, Any]:
        """Who is using the app right now: everyone seen in any live signal (and, once the app
        sends heartbeats, everyone with the app open), each person counted once."""
        bucket = int(now.timestamp()) // LIVE_CACHE_SECONDS
        signals = self._cache.get_or_set(
            ("live", bucket), lambda: self._call("live", lambda: self._source.live_user_ids(now))
        )
        everyone = set().union(*signals.values(), app_open or set())
        return {
            "online_now": len(everyone),
            "app_open": len(app_open) if app_open is not None else None,
            "using_app": len(signals.get("app", ())),
            "watching_video": len(signals.get("video", ())),
            "in_live_tutor": len(signals.get("tutor", ())),
            "asking_doubts": len(signals.get("doubts", ())),
            "taking_test": len(signals.get("tests", ())),
            "online_window_minutes": int(ONLINE_WINDOW.total_seconds() // 60),
            "checked_at": now,
        }
