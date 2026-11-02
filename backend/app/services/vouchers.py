"""Voucher management: Course and Live Tutor voucher CRUD, activate/deactivate, lock/unlock,
voucher-holder capacity, validation, redemption, summaries, analytics and the voucher audit
trail. Every rule is enforced here (server-side), never only in the UI.

Business rules (simplest safe defaults; see docs/VOUCHERS.md):
  * A voucher's category (course / live_tutor) is fixed at creation.
  * Course vouchers apply to listed subjects or to all subjects; Live Tutor vouchers apply to
    listed packages (beginner / intermediate / pro) or to all packages.
  * Discounts are a percentage (0-100] or a fixed amount, never more than the order amount.
  * A locked voucher cannot be edited, (de)activated, deleted or redeemed until unlocked.
  * A voucher can be deleted only if it is unlocked and has never been redeemed; otherwise
    deactivate it (history and analytics stay intact).
  * Once redeemed, a voucher's code cannot change (integrations rely on it).
  * Limits (total, per user, unique users) cannot be lowered below what is already used.
  * One order (order_reference) can redeem a given voucher once.
  * The user limit cannot be set below the number of registered voucher users.
  * A voucher user can be deleted only if they never redeemed a voucher.
Concurrency: user registration is one INSERT ... SELECT guarded by the limit, and redemption
starts with one conditional UPDATE of the use counter (which also takes SQLite's write lock),
so parallel requests can never exceed the user limit, a voucher's total limit, its per-user
limit or its unique-user limit.
"""

from __future__ import annotations

import re
import secrets
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import (
    String,
    and_,
    cast,
    exists,
    func,
    insert,
    literal,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import Settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.money import minor_units_for
from app.core.time import utcnow
from app.db.session import transaction
from app.db.types import UTCDateTime
from app.models import AuditLog, Voucher, VoucherTarget, VoucherUsageEvent, VoucherUser
from app.models.enums import (
    Actor,
    TutorPackage,
    VoucherActor,
    VoucherCategory,
    VoucherDiscountType,
    VoucherEventOutcome,
    VoucherEventSource,
)
from app.repositories.audit import AuditLogRepository
from app.repositories.settings import SettingsRepository
from app.schemas.voucher import (
    AnalyticsRead,
    AuditEntryRead,
    AuditPage,
    Capacity,
    CheckResult,
    MoneyTotal,
    RedeemRequest,
    RedeemResult,
    SubjectList,
    UsageBucket,
    UsageEventPage,
    UsageEventRead,
    VoucherCheckRequest,
    VoucherCreate,
    VoucherPage,
    VoucherRead,
    VoucherStatus,
    VoucherSummary,
    VoucherUpdate,
    VoucherUsageRow,
    VoucherUserCreate,
    VoucherUserPage,
    VoucherUserRead,
    _VoucherFields,
)
from app.services.context import RequestContext

USER_LIMIT_KEY = "voucher_user_limit"
AUDIT_ENTITIES = ("voucher", "voucher_user", "voucher_settings")
EDITABLE_FIELDS = (
    "code",
    "name",
    "description",
    "discount_type",
    "discount_value",
    "currency",
    "applies_to_all",
    "min_purchase_amount",
    "benefit_validity_days",
    "max_uses",
    "max_uses_per_user",
    "max_unique_users",
    "valid_from",
    "valid_until",
    "is_active",
)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
Granularity = Literal["day", "week", "month"]
MAX_BUCKETS = {"day": 400, "week": 160, "month": 60}
DEFAULT_SPAN = {"day": 30, "week": 12, "month": 12}
SortKey = Literal[
    "created",
    "code",
    "discount_value",
    "valid_from",
    "valid_until",
    "benefit_days",
    "max_uses",
    "use_count",
    "unique_users",
    "last_used",
]
MESSAGES = {
    "not_found": "No voucher with this code",
    "user_not_registered": "This user is not registered for vouchers",
    "locked": "This voucher is locked",
    "inactive": "This voucher is not active",
    "not_started": "This voucher is not valid yet",
    "expired": "This voucher has expired",
    "exhausted": "This voucher has reached its total redemption limit",
    "per_user_limit": "This user has already used this voucher the maximum number of times",
    "unique_user_limit": "This voucher has reached its maximum number of users",
    "wrong_category": "This voucher is for a different product category",
    "not_applicable": "This voucher does not apply to the selected subject or package",
    "below_minimum": "The order amount is below this voucher's minimum purchase amount",
    "currency_mismatch": "The order currency does not match the voucher's currency",
    "order_already_redeemed": "This order has already used this voucher",
}
STATUS_REASON = {
    "locked": "locked",
    "inactive": "inactive",
    "expired": "expired",
    "scheduled": "not_started",
    "exhausted": "exhausted",
}


@dataclass(frozen=True, slots=True)
class VoucherContext:
    """Who is acting. The owner (dashboard session or owner bearer token) or the voucher
    integration token. Audit rows from the integration use actor=system, via=voucher_api,
    because the shared audit table predates this module."""

    actor: VoucherActor
    request: RequestContext

    @property
    def audit_actor(self) -> Actor:
        return Actor.OWNER if self.actor is VoucherActor.OWNER else Actor.SYSTEM

    @property
    def source(self) -> VoucherEventSource:
        return (
            VoucherEventSource.API
            if self.actor is VoucherActor.VOUCHER_API
            else VoucherEventSource.DASHBOARD
        )


class _Refused(Exception):  # noqa: N818 - internal control flow
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason


def _like(q: str | None) -> str | None:
    if not q or not q.strip():
        return None
    if CONTROL_CHARS.search(q):
        raise DomainValidationError(
            "Search text contains control characters", code="invalid_search"
        )
    needle = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{needle}%"


def _key(value: str) -> str:
    return value.casefold()[:80]


def _field_error(field: str, message: str, code: str = "validation_error") -> DomainValidationError:
    """A 422 shaped like request validation errors, so the form shows it next to the field."""
    return DomainValidationError(
        message, code=code, details=[{"loc": ["body", field], "msg": message, "type": code}]
    )


def discount_for(
    discount_type: VoucherDiscountType, value: Decimal, order_amount: Decimal, currency: str
) -> Decimal:
    """Percentage of the order (rounded half-up to the currency's minor unit) or a fixed
    amount; never more than the order itself."""
    quantum = Decimal(1).scaleb(-minor_units_for(currency))
    if discount_type is VoucherDiscountType.PERCENTAGE:
        amount = (order_amount * value / Decimal(100)).quantize(quantum, rounding=ROUND_HALF_UP)
    else:
        amount = value
    return min(amount, order_amount)


def status_of(v: Voucher, now: datetime) -> VoucherStatus:
    """Precedence: locked > inactive > expired > scheduled > exhausted > active."""
    if v.is_locked:
        return "locked"
    if not v.is_active:
        return "inactive"
    if v.valid_until is not None and v.valid_until <= now:
        return "expired"
    if v.valid_from is not None and v.valid_from > now:
        return "scheduled"
    if v.max_uses is not None and v.use_count >= v.max_uses:
        return "exhausted"
    return "active"


def status_clause(status: VoucherStatus, now: datetime):  # type: ignore[no-untyped-def]
    """SQL equivalent of status_of (same precedence), for filtering and counting."""
    v = Voucher
    open_ = and_(not_(v.is_locked), v.is_active)
    started = or_(v.valid_from.is_(None), v.valid_from <= now)
    not_expired = or_(v.valid_until.is_(None), v.valid_until > now)
    return {
        "locked": v.is_locked,
        "inactive": and_(not_(v.is_locked), not_(v.is_active)),
        "expired": and_(open_, v.valid_until.is_not(None), v.valid_until <= now),
        "scheduled": and_(open_, not_expired, v.valid_from.is_not(None), v.valid_from > now),
        "exhausted": and_(
            open_, started, not_expired, v.max_uses.is_not(None), v.use_count >= v.max_uses
        ),
        "active": and_(
            open_, started, not_expired, or_(v.max_uses.is_(None), v.use_count < v.max_uses)
        ),
    }[status]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if hasattr(value, "value"):  # enums
        return value.value
    return value


def _snapshot(v: Voucher) -> dict[str, Any]:
    out: dict[str, Any] = {f: _jsonable(getattr(v, f)) for f in EDITABLE_FIELDS}
    out["applies_to"] = [t.value for t in v.targets]
    return out


class VoucherService:
    def __init__(self, session: Session, settings: Settings, clock=utcnow) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.settings = settings
        self.clock = clock
        self.audit_repo = AuditLogRepository(session)
        self.settings_repo = SettingsRepository(session)

    # --- helpers -------------------------------------------
    def _audit(
        self,
        ctx: VoucherContext,
        action: str,
        entity_type: str,
        entity_id: int | str | None,
        changes: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(changes or {})
        if ctx.actor is VoucherActor.VOUCHER_API:
            payload["via"] = "voucher_api"
        self.audit_repo.record(
            actor=ctx.audit_actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=ctx.request.request_id,
            client_ip=ctx.request.client_ip,
            changes=payload or None,
        )

    def _read(self, v: Voucher, now: datetime | None = None) -> VoucherRead:
        now = now or self.clock()
        values = [t.value for t in v.targets]
        course = v.category is VoucherCategory.COURSE
        return VoucherRead(
            id=v.id,
            category=v.category,
            code=v.code,
            name=v.name,
            description=v.description,
            discount_type=v.discount_type,
            discount_value=v.discount_value,
            currency=v.currency,
            applies_to_all=v.applies_to_all,
            subjects=values if course else [],
            packages=[] if course else [p for p in TutorPackage if p.value in values],
            min_purchase_amount=v.min_purchase_amount,
            benefit_validity_days=v.benefit_validity_days,
            max_uses=v.max_uses,
            max_uses_per_user=v.max_uses_per_user,
            max_unique_users=v.max_unique_users,
            valid_from=v.valid_from,
            valid_until=v.valid_until,
            is_active=v.is_active,
            status=status_of(v, now),
            use_count=v.use_count,
            remaining_uses=None if v.max_uses is None else max(v.max_uses - v.use_count, 0),
            unique_user_count=v.unique_user_count,
            remaining_unique_users=(
                None
                if v.max_unique_users is None
                else max(v.max_unique_users - v.unique_user_count, 0)
            ),
            last_used_at=v.last_used_at,
            is_locked=v.is_locked,
            lock_reason=v.lock_reason,
            locked_at=v.locked_at,
            locked_by=v.locked_by,
            unlocked_at=v.unlocked_at,
            unlocked_by=v.unlocked_by,
            created_at=v.created_at,
            created_by=v.created_by,
            updated_at=v.updated_at,
            updated_by=v.updated_by,
            version=v.version,
            can_delete=not v.is_locked and v.use_count == 0,
        )

    def _require(self, voucher_id: int, category: VoucherCategory | None = None) -> Voucher:
        v = self.session.get(Voucher, voucher_id)
        if v is None or (category is not None and v.category is not category):
            raise NotFoundError(f"Voucher {voucher_id} not found", code="voucher_not_found")
        return v

    def _targets(self, category: VoucherCategory, body: _VoucherFields) -> list[str]:
        """What the voucher applies to, checked against its category. Empty = all."""
        if body.currency not in self.settings.supported_currencies:
            raise _field_error(
                "currency",
                f"Use one of: {', '.join(self.settings.supported_currencies)}",
                "unsupported_currency",
            )
        if category is VoucherCategory.COURSE:
            if body.packages:
                raise _field_error("packages", "Course vouchers apply to subjects, not packages")
            values = body.subjects
            field, noun = "subjects", "subject"
        else:
            if body.subjects:
                raise _field_error(
                    "subjects", "Live Tutor vouchers apply to packages, not subjects"
                )
            values = [p.value for p in body.packages]
            field, noun = "packages", "package"
        if body.applies_to_all and values:
            raise _field_error(
                field, f"Leave the {noun} list empty when the voucher applies to all"
            )
        if not body.applies_to_all and not values:
            raise _field_error(field, f"Choose at least one {noun}, or apply the voucher to all")
        return values

    @staticmethod
    def _set_targets(v: Voucher, values: list[str]) -> bool:
        """Replace the voucher's targets by difference (no delete+insert of the same key, which
        would trip the unique constraint inside one flush). Returns True if anything changed."""
        wanted = {_key(x): x for x in values}
        changed = False
        for t in list(v.targets):
            if t.value_key in wanted:
                new_value = wanted.pop(t.value_key)
                if t.value != new_value:
                    t.value, changed = new_value, True
            else:
                v.targets.remove(t)
                changed = True
        for key, value in wanted.items():
            v.targets.append(VoucherTarget(value=value, value_key=key))
            changed = True
        return changed

    # --- vouchers: read -------------------------------------------
    def list(
        self,
        category: VoucherCategory,
        *,
        q: str | None = None,
        status: VoucherStatus | None = None,
        discount_type: VoucherDiscountType | None = None,
        applies_to: str | None = None,
        valid_start: datetime | None = None,
        valid_end: datetime | None = None,
        sort: SortKey = "created",
        order: str = "desc",
        limit: int = 25,
        offset: int = 0,
    ) -> VoucherPage:
        now = self.clock()
        V, T = Voucher, VoucherTarget
        query = select(V).where(V.category == category)
        pattern = _like(q)
        if pattern:
            query = query.where(
                or_(
                    func.lower(V.code).like(pattern, escape="\\"),
                    func.lower(V.name).like(pattern, escape="\\"),
                    func.lower(func.coalesce(V.description, "")).like(pattern, escape="\\"),
                    exists(
                        select(T.id).where(
                            T.voucher_id == V.id, T.value_key.like(pattern, escape="\\")
                        )
                    ),
                )
            )
        if status:
            query = query.where(status_clause(status, now))
        if discount_type:
            query = query.where(V.discount_type == discount_type)
        if applies_to and applies_to.strip():
            # Vouchers usable for this subject / package: listed explicitly, or "all".
            key = _key(" ".join(applies_to.split()))
            query = query.where(
                or_(
                    V.applies_to_all,
                    exists(select(T.id).where(T.voucher_id == V.id, T.value_key == key)),
                )
            )
        # Campaign window overlaps [valid_start, valid_end).
        if valid_end is not None:
            query = query.where(or_(V.valid_from.is_(None), V.valid_from < valid_end))
        if valid_start is not None:
            query = query.where(or_(V.valid_until.is_(None), V.valid_until > valid_start))
        column = {
            "created": V.created_at,
            "code": V.code,
            "discount_value": V.discount_value,
            "valid_from": V.valid_from,
            "valid_until": V.valid_until,
            "benefit_days": V.benefit_validity_days,
            "max_uses": V.max_uses,
            "use_count": V.use_count,
            "unique_users": V.unique_user_count,
            "last_used": V.last_used_at,
        }[sort]
        ordering = column.asc() if order == "asc" else column.desc()
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(ordering, V.id.desc()).limit(limit).offset(offset)
        )
        return VoucherPage(
            items=[self._read(v, now) for v in rows], total=total, limit=limit, offset=offset
        )

    def get(self, voucher_id: int, category: VoucherCategory | None = None) -> VoucherRead:
        return self._read(self._require(voucher_id, category))

    def subjects(self) -> SubjectList:
        """Subject names already used by Course vouchers (suggestions for the form)."""
        T, V = VoucherTarget, Voucher
        rows = self.session.execute(
            select(T.value_key, func.min(T.value))
            .join(V, V.id == T.voucher_id)
            .where(V.category == VoucherCategory.COURSE)
            .group_by(T.value_key)
            .order_by(T.value_key)
            .limit(500)
        ).all()
        return SubjectList(items=[value for _key_, value in rows])

    # --- vouchers: write -------------------------------------------
    def create(
        self, category: VoucherCategory, body: VoucherCreate, ctx: VoucherContext
    ) -> VoucherRead:
        targets = self._targets(category, body)
        generated = body.code is None
        for _attempt in range(5):
            code = body.code or "VCH-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            v = Voucher(
                category=category,
                code=code,
                name=body.name or code,
                description=body.description,
                discount_type=body.discount_type,
                discount_value=body.discount_value,
                currency=body.currency,
                applies_to_all=body.applies_to_all,
                min_purchase_amount=body.min_purchase_amount,
                benefit_validity_days=body.benefit_validity_days,
                max_uses=body.max_uses,
                max_uses_per_user=body.max_uses_per_user,
                max_unique_users=body.max_unique_users,
                valid_from=body.valid_from,
                valid_until=body.valid_until,
                is_active=body.is_active,
                use_count=0,
                unique_user_count=0,
                is_locked=False,
                created_by=ctx.actor,
                updated_by=ctx.actor,
            )
            v.targets = [VoucherTarget(value=x, value_key=_key(x)) for x in targets]
            try:
                with transaction(self.session):
                    self.session.add(v)
                    self.session.flush()
                    self._audit(
                        ctx,
                        "voucher.create",
                        "voucher",
                        v.id,
                        {"category": category.value, "after": _snapshot(v)},
                    )
                return self._read(v)
            except IntegrityError:
                if not generated:
                    raise ConflictError(
                        f"A voucher with code {code} already exists", code="duplicate_voucher"
                    ) from None
        raise ConflictError("Could not generate a unique code; try again", code="duplicate_voucher")

    def update(
        self,
        category: VoucherCategory,
        voucher_id: int,
        body: VoucherUpdate,
        ctx: VoucherContext,
    ) -> VoucherRead:
        targets = self._targets(category, body)
        try:
            with transaction(self.session):
                v = self._require(voucher_id, category)
                if v.is_locked:
                    raise ConflictError(
                        "Unlock the voucher before editing it", code="voucher_locked"
                    )
                if body.version != v.version:
                    raise ConflictError(
                        "This voucher changed since you opened it; reload and retry",
                        code="concurrent_modification",
                    )
                if body.code != v.code and v.use_count > 0:
                    raise ConflictError(
                        "The code of a voucher that has been used cannot change",
                        code="code_in_use",
                    )
                if body.max_uses is not None and body.max_uses < v.use_count:
                    raise _field_error(
                        "max_uses",
                        f"Cannot be below the redemptions so far ({v.use_count})",
                        "max_uses_below_use_count",
                    )
                if (
                    body.max_unique_users is not None
                    and body.max_unique_users < v.unique_user_count
                ):
                    raise _field_error(
                        "max_unique_users",
                        f"Cannot be below the users reached so far ({v.unique_user_count})",
                        "max_unique_users_below_count",
                    )
                before = _snapshot(v)
                values = body.model_dump(include=set(EDITABLE_FIELDS))
                values["name"] = body.name or body.code
                for f in EDITABLE_FIELDS:
                    setattr(v, f, values[f])
                self._set_targets(v, targets)
                v.updated_by = ctx.actor
                flag_modified(v, "updated_by")  # always bump version/updated_at
                after = _snapshot(v)
                changed = {
                    k: {"before": before[k], "after": after[k]}
                    for k in after
                    if before[k] != after[k]
                }
                self.session.flush()
                self._audit(
                    ctx,
                    "voucher.update",
                    "voucher",
                    v.id,
                    {"category": category.value, "code": v.code, "changed": changed},
                )
        except StaleDataError as exc:
            raise ConflictError(
                "This voucher changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        except IntegrityError as exc:
            raise ConflictError(
                f"A voucher with code {body.code} already exists", code="duplicate_voucher"
            ) from exc
        return self._read(v)

    def delete(self, category: VoucherCategory, voucher_id: int, ctx: VoucherContext) -> None:
        with transaction(self.session):
            v = self._require(voucher_id, category)
            if v.is_locked:
                raise ConflictError("Unlock the voucher before deleting it", code="voucher_locked")
            if v.use_count > 0:
                raise ConflictError(
                    "A voucher that has been used cannot be deleted; deactivate it instead",
                    code="voucher_in_use",
                )
            snapshot = _snapshot(v)
            self.session.delete(v)
            self.session.flush()
            self._audit(
                ctx,
                "voucher.delete",
                "voucher",
                voucher_id,
                {"category": category.value, "before": snapshot},
            )

    def set_active(
        self, category: VoucherCategory, voucher_id: int, active: bool, ctx: VoucherContext
    ) -> VoucherRead:
        try:
            with transaction(self.session):
                v = self._require(voucher_id, category)
                if v.is_locked:
                    raise ConflictError(
                        "Unlock the voucher before changing its status", code="voucher_locked"
                    )
                if v.is_active == active:
                    raise ConflictError(
                        "Voucher is already active" if active else "Voucher is already inactive",
                        code="already_active" if active else "already_inactive",
                    )
                v.is_active = active
                v.updated_by = ctx.actor
                self.session.flush()
                self._audit(
                    ctx,
                    "voucher.activate" if active else "voucher.deactivate",
                    "voucher",
                    v.id,
                    {"category": category.value, "code": v.code},
                )
        except StaleDataError as exc:
            raise ConflictError(
                "This voucher changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        return self._read(v)

    def set_lock(
        self,
        category: VoucherCategory,
        voucher_id: int,
        locked: bool,
        reason: str | None,
        ctx: VoucherContext,
    ) -> VoucherRead:
        try:
            with transaction(self.session):
                v = self._require(voucher_id, category)
                if v.is_locked == locked:
                    raise ConflictError(
                        "Voucher is already locked" if locked else "Voucher is not locked",
                        code="already_locked" if locked else "not_locked",
                    )
                now = self.clock()
                if locked:
                    v.is_locked, v.lock_reason, v.locked_at, v.locked_by = (
                        True,
                        reason,
                        now,
                        ctx.actor,
                    )
                else:
                    reason = v.lock_reason
                    v.is_locked, v.lock_reason = False, None
                    v.unlocked_at, v.unlocked_by = now, ctx.actor
                v.updated_by = ctx.actor
                self.session.flush()
                self._audit(
                    ctx,
                    "voucher.lock" if locked else "voucher.unlock",
                    "voucher",
                    v.id,
                    {"category": category.value, "code": v.code, "reason": reason},
                )
        except StaleDataError as exc:
            raise ConflictError(
                "This voucher changed at the same time; reload and retry",
                code="concurrent_modification",
            ) from exc
        return self._read(v)

    # --- users and capacity -------------------------------------------
    def user_limit(self) -> int:
        row = self.settings_repo.get(USER_LIMIT_KEY)
        if row is not None and isinstance(row.value, int) and row.value >= 1:
            return row.value
        return self.settings.voucher_default_user_limit

    def capacity(self) -> Capacity:
        limit = self.user_limit()
        total = self.session.scalar(select(func.count()).select_from(VoucherUser)) or 0
        return Capacity(
            user_limit=limit,
            total_users=total,
            remaining=max(limit - total, 0),
            percent_used=round(min(total / limit, 1.0) * 100, 1),
            default_limit=self.settings.voucher_default_user_limit,
        )

    def set_user_limit(self, new_limit: int, ctx: VoucherContext) -> Capacity:
        with transaction(self.session):
            before = self.user_limit()
            total = self.session.scalar(select(func.count()).select_from(VoucherUser)) or 0
            if new_limit < total:
                raise ConflictError(
                    f"The limit cannot be below the {total} users already registered",
                    code="limit_below_current",
                    details={"total_users": total},
                )
            self.settings_repo.upsert(USER_LIMIT_KEY, new_limit, ctx.audit_actor)
            self._audit(
                ctx,
                "voucher.user_limit",
                "voucher_settings",
                USER_LIMIT_KEY,
                {"before": before, "after": new_limit},
            )
        return self.capacity()

    def list_users(self, *, q: str | None, limit: int, offset: int) -> VoucherUserPage:
        E = VoucherUsageEvent
        uses = (
            select(E.voucher_user_id, func.count().label("n"))
            .where(E.outcome == VoucherEventOutcome.SUCCESS)
            .group_by(E.voucher_user_id)
            .subquery()
        )
        query = select(VoucherUser, func.coalesce(uses.c.n, 0)).outerjoin(
            uses, uses.c.voucher_user_id == VoucherUser.id
        )
        pattern = _like(q)
        if pattern:
            query = query.where(
                or_(
                    func.lower(VoucherUser.external_id).like(pattern, escape="\\"),
                    func.lower(func.coalesce(VoucherUser.display_name, "")).like(
                        pattern, escape="\\"
                    ),
                )
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.execute(
            query.order_by(VoucherUser.created_at.desc(), VoucherUser.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return VoucherUserPage(
            items=[
                VoucherUserRead(
                    id=u.id,
                    external_id=u.external_id,
                    display_name=u.display_name,
                    created_at=u.created_at,
                    created_by=u.created_by,
                    redemptions=int(n),
                )
                for u, n in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def register_user(self, body: VoucherUserCreate, ctx: VoucherContext) -> VoucherUserRead:
        """Atomic: the row is inserted only while the count is below the limit, in a single
        statement, so concurrent registrations can never exceed the limit."""
        limit = self.user_limit()
        now = self.clock()
        count_now = select(func.count()).select_from(VoucherUser).scalar_subquery()
        stmt = insert(VoucherUser).from_select(
            ["external_id", "display_name", "created_at", "created_by"],
            select(
                literal(body.external_id, String()),
                literal(body.display_name, String()),
                literal(now, UTCDateTime()),
                literal(ctx.actor.value, String()),
            ).where(count_now < limit),
        )
        try:
            with transaction(self.session):
                result = self.session.execute(stmt)
                if result.rowcount != 1:
                    exists_ = self.session.scalar(
                        select(VoucherUser.id).where(VoucherUser.external_id == body.external_id)
                    )
                    if exists_ is not None:  # a duplicate, not a capacity problem
                        raise IntegrityError("duplicate external_id", None, Exception())
                    raise ConflictError(
                        f"User limit reached ({limit}); raise the limit to add more users",
                        code="user_limit_reached",
                        details={"user_limit": limit},
                    )
                user = self.session.scalar(
                    select(VoucherUser).where(VoucherUser.external_id == body.external_id)
                )
                assert user is not None  # noqa: S101 - just inserted in this transaction
                self._audit(
                    ctx,
                    "voucher_user.register",
                    "voucher_user",
                    user.id,
                    {"external_id": user.external_id},
                )
        except IntegrityError as exc:
            raise ConflictError(
                f"User {body.external_id} is already registered", code="duplicate_user"
            ) from exc
        return VoucherUserRead.model_validate(user)

    def delete_user(self, user_id: int, ctx: VoucherContext) -> None:
        with transaction(self.session):
            user = self.session.get(VoucherUser, user_id)
            if user is None:
                raise NotFoundError(f"User {user_id} not found", code="voucher_user_not_found")
            used = self.session.scalar(
                select(func.count())
                .select_from(VoucherUsageEvent)
                .where(
                    VoucherUsageEvent.voucher_user_id == user_id,
                    VoucherUsageEvent.outcome == VoucherEventOutcome.SUCCESS,
                )
            )
            if used:
                raise ConflictError(
                    "A user who has redeemed vouchers cannot be deleted", code="user_has_usage"
                )
            external_id = user.external_id
            self.session.delete(user)
            self.session.flush()
            self._audit(
                ctx, "voucher_user.delete", "voucher_user", user_id, {"external_id": external_id}
            )

    # --- validation and redemption -------------------------------------------
    @staticmethod
    def _offer_reason(v: Voucher, body: VoucherCheckRequest) -> str | None:
        """Rules about what is being bought (not about counters or dates)."""
        if v.category is not body.category:
            return "wrong_category"
        if not v.applies_to_all and _key(body.item) not in {t.value_key for t in v.targets}:
            return "not_applicable"
        if body.currency != v.currency:
            return "currency_mismatch"
        if v.min_purchase_amount is not None and body.order_amount < v.min_purchase_amount:
            return "below_minimum"
        return None

    def _successes(self, voucher_id: int, user_id: int) -> int:
        E = VoucherUsageEvent
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(E)
                .where(
                    E.voucher_id == voucher_id,
                    E.voucher_user_id == user_id,
                    E.outcome == VoucherEventOutcome.SUCCESS,
                )
            )
            or 0
        )

    def _benefit_until(self, v: Voucher, at: datetime) -> datetime | None:
        days = v.benefit_validity_days
        return None if days is None else at + timedelta(days=days)

    def check(self, body: VoucherCheckRequest) -> CheckResult:
        """Read-only: would this redemption succeed right now, and for what discount? Nothing
        is recorded. Redemption re-checks everything atomically."""
        now = self.clock()
        v = self.session.scalar(select(Voucher).where(Voucher.code == body.code))
        user = self.session.scalar(
            select(VoucherUser).where(VoucherUser.external_id == body.user_external_id)
        )
        reason: str | None
        if v is None:
            reason = "not_found"
        else:
            reason = STATUS_REASON.get(status_of(v, now)) or self._offer_reason(v, body)
            if reason is None and user is None:
                reason = "user_not_registered"
            if reason is None and user is not None:
                used = self._successes(v.id, user.id)
                if v.max_uses_per_user is not None and used >= v.max_uses_per_user:
                    reason = "per_user_limit"
                elif (
                    used == 0
                    and v.max_unique_users is not None
                    and v.unique_user_count >= v.max_unique_users
                ):
                    reason = "unique_user_limit"
        discount = (
            discount_for(v.discount_type, v.discount_value, body.order_amount, body.currency)
            if v is not None and reason is None
            else Decimal(0)
        )
        return CheckResult(
            valid=reason is None,
            reason=reason,  # type: ignore[arg-type]
            message=MESSAGES[reason] if reason else "The voucher can be applied",
            code=body.code,
            category=body.category,
            voucher_id=v.id if v is not None else None,
            discount_type=v.discount_type if v is not None else None,
            discount_value=v.discount_value if v is not None else None,
            currency=body.currency,
            order_amount=body.order_amount,
            discount_amount=discount,
            final_amount=body.order_amount - discount,
            benefit_expires_at=(
                self._benefit_until(v, now) if v is not None and reason is None else None
            ),
        )

    def redeem(self, body: RedeemRequest, ctx: VoucherContext) -> RedeemResult:
        voucher = self.session.scalar(select(Voucher).where(Voucher.code == body.code))
        user = self.session.scalar(
            select(VoucherUser).where(VoucherUser.external_id == body.user_external_id)
        )
        self.session.rollback()  # the guarded write below must start its own transaction
        if voucher is None:
            self._refuse(body, None, None, None, "not_found", ctx)
        assert voucher is not None  # noqa: S101 - _refuse always raises
        category = voucher.category
        if user is None:
            self._refuse(body, voucher.id, category, None, "user_not_registered", ctx)
        assert user is not None  # noqa: S101
        voucher_id, user_id = voucher.id, user.id
        now = self.clock()
        V, E = Voucher, VoucherUsageEvent
        try:
            with transaction(self.session):
                # Guard + increment in one statement; this also takes the write lock, so the
                # checks below see a stable state until commit.
                result = self.session.execute(
                    update(V)
                    .where(
                        V.id == voucher_id,
                        not_(V.is_locked),
                        V.is_active,
                        or_(V.valid_from.is_(None), V.valid_from <= now),
                        or_(V.valid_until.is_(None), V.valid_until > now),
                        or_(V.max_uses.is_(None), V.use_count < V.max_uses),
                    )
                    .values(use_count=V.use_count + 1, last_used_at=now, version=V.version + 1)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise _Refused()
                v = self.session.get(V, voucher_id, populate_existing=True)
                assert v is not None  # noqa: S101 - just updated
                offer = self._offer_reason(v, body)
                if offer:
                    raise _Refused(offer)
                same_order = self.session.scalar(
                    select(func.count())
                    .select_from(E)
                    .where(
                        E.voucher_id == voucher_id,
                        E.order_reference == body.order_reference,
                        E.outcome == VoucherEventOutcome.SUCCESS,
                    )
                )
                if same_order:
                    raise _Refused("order_already_redeemed")
                used = self._successes(voucher_id, user_id)
                if v.max_uses_per_user is not None and used >= v.max_uses_per_user:
                    raise _Refused("per_user_limit")
                if used == 0:  # a new user for this voucher
                    counted = self.session.execute(
                        update(V)
                        .where(
                            V.id == voucher_id,
                            or_(
                                V.max_unique_users.is_(None),
                                V.unique_user_count < V.max_unique_users,
                            ),
                        )
                        .values(unique_user_count=V.unique_user_count + 1)
                        .execution_options(synchronize_session=False)
                    )
                    if counted.rowcount != 1:
                        raise _Refused("unique_user_limit")
                discount = discount_for(
                    v.discount_type, v.discount_value, body.order_amount, body.currency
                )
                benefit_until = self._benefit_until(v, now)
                event = E(
                    voucher_id=voucher_id,
                    voucher_code=body.code,
                    category=category,
                    voucher_user_id=user_id,
                    user_external_id=body.user_external_id,
                    item=body.item,
                    order_reference=body.order_reference,
                    order_amount=body.order_amount,
                    discount_amount=discount,
                    currency=body.currency,
                    benefit_expires_at=benefit_until,
                    outcome=VoucherEventOutcome.SUCCESS,
                    source=ctx.source,
                    actor=ctx.actor,
                    request_id=ctx.request.request_id,
                    occurred_at=now,
                )
                self.session.add(event)
                self.session.flush()
                self._audit(
                    ctx,
                    "voucher.redeem",
                    "voucher",
                    voucher_id,
                    {
                        "category": category.value,
                        "code": body.code,
                        "user": body.user_external_id,
                        "item": body.item,
                        "order": body.order_reference,
                        "discount": _jsonable(discount),
                        "currency": body.currency,
                        "event_id": event.id,
                    },
                )
                use_count, max_uses, unique_users = self.session.execute(
                    select(V.use_count, V.max_uses, V.unique_user_count).where(V.id == voucher_id)
                ).one()
        except _Refused as refused:
            reason = refused.reason or self._refusal_reason(voucher_id, now)
            self._refuse(body, voucher_id, category, user_id, reason, ctx)
        return RedeemResult(
            event_id=event.id,
            voucher_id=voucher_id,
            code=body.code,
            category=category,
            user_external_id=body.user_external_id,
            item=body.item,
            order_reference=body.order_reference,
            currency=body.currency,
            order_amount=body.order_amount,
            discount_amount=discount,
            final_amount=body.order_amount - discount,
            benefit_expires_at=benefit_until,
            use_count=use_count,
            remaining_uses=None if max_uses is None else max(max_uses - use_count, 0),
            unique_user_count=unique_users,
            occurred_at=now,
        )

    def _refusal_reason(self, voucher_id: int, now: datetime) -> str:
        v = self.session.get(Voucher, voucher_id, populate_existing=True)
        if v is None:
            return "not_found"
        return STATUS_REASON.get(status_of(v, now), "exhausted")

    def _refuse(
        self,
        body: RedeemRequest,
        voucher_id: int | None,
        category: VoucherCategory | None,
        user_id: int | None,
        reason: str,
        ctx: VoucherContext,
    ):  # type: ignore[no-untyped-def]
        """Record the failed attempt (its own transaction), then raise."""
        with transaction(self.session):
            event = VoucherUsageEvent(
                voucher_id=voucher_id,
                voucher_code=body.code[:40],
                category=category or body.category,
                voucher_user_id=user_id,
                user_external_id=body.user_external_id[:128],
                item=body.item,
                order_reference=body.order_reference,
                order_amount=body.order_amount,
                currency=body.currency,
                outcome=VoucherEventOutcome.FAILED,
                failure_reason=reason,
                source=ctx.source,
                actor=ctx.actor,
                request_id=ctx.request.request_id,
                occurred_at=self.clock(),
            )
            self.session.add(event)
            self.session.flush()
            self._audit(
                ctx,
                "voucher.redeem_failed",
                "voucher",
                voucher_id,
                {
                    "category": (category or body.category).value,
                    "code": body.code,
                    "user": body.user_external_id,
                    "order": body.order_reference,
                    "reason": reason,
                },
            )
            event_id = event.id
        details = {"reason": reason, "event_id": event_id}
        if reason in ("not_found", "user_not_registered"):
            raise NotFoundError(
                MESSAGES[reason],
                code="voucher_not_found" if reason == "not_found" else "voucher_user_not_found",
                details=details,
            )
        raise ConflictError(MESSAGES[reason], code=f"voucher_{reason}", details=details)

    def usage_events(
        self,
        *,
        voucher_id: int | None = None,
        category: VoucherCategory | None = None,
        outcome: VoucherEventOutcome | None = None,
        q: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UsageEventPage:
        E = VoucherUsageEvent
        query = select(E)
        if voucher_id is not None:
            query = query.where(E.voucher_id == voucher_id)
        if category is not None:
            query = query.where(E.category == category)
        if outcome is not None:
            query = query.where(E.outcome == outcome)
        if start is not None:
            query = query.where(E.occurred_at >= start)
        if end is not None:
            query = query.where(E.occurred_at < end)
        pattern = _like(q)
        if pattern:
            query = query.where(
                or_(
                    func.lower(func.coalesce(E.user_external_id, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(E.order_reference, "")).like(pattern, escape="\\"),
                    func.lower(E.voucher_code).like(pattern, escape="\\"),
                )
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(E.occurred_at.desc(), E.id.desc()).limit(limit).offset(offset)
        )
        return UsageEventPage(
            items=[UsageEventRead.model_validate(e) for e in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    # --- summary and analytics -------------------------------------------
    def summary(self, category: VoucherCategory | None, timezone: str) -> VoucherSummary:
        now = self.clock()
        tz = ZoneInfo(timezone)
        in_category = [] if category is None else [Voucher.category == category]
        counts = {
            s: self.session.scalar(
                select(func.count()).select_from(Voucher).where(status_clause(s, now), *in_category)  # type: ignore[arg-type]
            )
            or 0
            for s in ("active", "locked", "inactive", "expired", "exhausted", "scheduled")
        }
        total = (
            self.session.scalar(select(func.count()).select_from(Voucher).where(*in_category)) or 0
        )
        local = now.astimezone(tz)
        today = datetime(local.year, local.month, local.day, tzinfo=tz)
        week = today - timedelta(days=today.weekday())
        month = today.replace(day=1)
        E = VoucherUsageEvent
        scope = [] if category is None else [E.category == category]
        success = [E.outcome == VoucherEventOutcome.SUCCESS, *scope]

        def count(*where: Any) -> int:
            return int(self.session.scalar(select(func.count()).select_from(E).where(*where)) or 0)

        unique_users = self.session.scalar(
            select(func.count(func.distinct(E.voucher_user_id))).where(
                *success, E.voucher_user_id.is_not(None)
            )
        )
        discounts = self.session.execute(
            select(E.currency, func.sum(E.discount_amount))
            .where(*success, E.currency.is_not(None), E.discount_amount.is_not(None))
            .group_by(E.currency)
            .order_by(E.currency)
        ).all()
        capacity = self.capacity()
        return VoucherSummary(
            category=category,
            total_vouchers=total,
            active=counts["active"],
            scheduled=counts["scheduled"],
            expired=counts["expired"],
            inactive=counts["inactive"],
            exhausted=counts["exhausted"],
            locked=counts["locked"],
            unlocked=total - counts["locked"],
            total_redemptions=count(*success),
            failed_redemptions=count(E.outcome == VoucherEventOutcome.FAILED, *scope),
            unique_users_reached=int(unique_users or 0),
            redemptions_today=count(*success, E.occurred_at >= today),
            redemptions_this_week=count(*success, E.occurred_at >= week),
            redemptions_this_month=count(*success, E.occurred_at >= month),
            discount_given=[
                MoneyTotal(currency=cur, amount=amount or Decimal(0)) for cur, amount in discounts
            ],
            total_users=capacity.total_users,
            user_limit=capacity.user_limit,
            remaining_capacity=capacity.remaining,
        )

    def _buckets(
        self, granularity: Granularity, start: datetime, end: datetime, tz: ZoneInfo
    ) -> list[tuple[datetime, datetime, str]]:
        def local_start(moment: datetime) -> date:
            d = moment.astimezone(tz).date()
            if granularity == "week":
                return d - timedelta(days=d.weekday())
            if granularity == "month":
                return d.replace(day=1)
            return d

        def advance(d: date) -> date:
            if granularity == "day":
                return d + timedelta(days=1)
            if granularity == "week":
                return d + timedelta(days=7)
            return date(d.year + (d.month == 12), d.month % 12 + 1, 1)

        def instant(d: date) -> datetime:
            return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(start.tzinfo)

        def label(d: date) -> str:
            if granularity == "month":
                return d.strftime("%b %Y")
            if granularity == "week":
                return f"Wk {d.strftime('%d %b')}"
            return d.strftime("%d %b")

        out: list[tuple[datetime, datetime, str]] = []
        d = local_start(start)
        while instant(d) < end:
            nxt = advance(d)
            out.append((instant(d), instant(nxt), label(d)))
            d = nxt
            if len(out) > MAX_BUCKETS[granularity]:
                raise DomainValidationError(
                    f"Range too long for {granularity} buckets (max {MAX_BUCKETS[granularity]})",
                    code="invalid_range",
                )
        return out

    def analytics(
        self,
        *,
        granularity: Granularity,
        start: datetime | None,
        end: datetime | None,
        category: VoucherCategory | None,
        voucher_id: int | None,
        timezone: str,
    ) -> AnalyticsRead:
        tz = ZoneInfo(timezone)
        now = self.clock()
        end = end or now
        if start is None:
            span = DEFAULT_SPAN[granularity]
            days = (
                span if granularity == "day" else span * 7 if granularity == "week" else span * 31
            )
            start = end - timedelta(days=days - 1)
        if end <= start:
            raise DomainValidationError("'end' must be after 'start'", code="invalid_range")
        if voucher_id is not None:
            self._require(voucher_id, category)
        buckets = self._buckets(granularity, start, end, tz)
        range_start, range_end = buckets[0][0], buckets[-1][1]

        E = VoucherUsageEvent
        events = select(E.occurred_at, E.outcome, E.voucher_id).where(
            E.occurred_at >= range_start, E.occurred_at < range_end
        )
        if category is not None:
            events = events.where(E.category == category)
        if voucher_id is not None:
            events = events.where(E.voucher_id == voucher_id)
        starts = [b[0] for b in buckets]
        success = [0] * len(buckets)
        failed = [0] * len(buckets)
        in_range: dict[int, int] = {}
        for occurred_at, outcome, vid in self.session.execute(events):
            i = bisect_right(starts, occurred_at) - 1
            if i < 0:
                continue
            if outcome == VoucherEventOutcome.SUCCESS:
                success[i] += 1
                if vid is not None:
                    in_range[vid] = in_range.get(vid, 0) + 1
            else:
                failed[i] += 1

        vouchers = select(Voucher)
        if category is not None:
            vouchers = vouchers.where(Voucher.category == category)
        if voucher_id is not None:
            vouchers = vouchers.where(Voucher.id == voucher_id)
        rows = [
            VoucherUsageRow(
                voucher_id=v.id,
                code=v.code,
                name=v.name,
                category=v.category,
                status=status_of(v, now),
                use_count=v.use_count,
                in_range=in_range.get(v.id, 0),
                last_used_at=v.last_used_at,
            )
            for v in self.session.scalars(vouchers)
        ]
        by_use = sorted(rows, key=lambda r: (-r.in_range, -r.use_count, r.code))
        return AnalyticsRead(
            granularity=granularity,
            timezone=timezone,
            start=range_start,
            end=range_end,
            category=category,
            voucher_id=voucher_id,
            summary=self.summary(category, timezone),
            series=[
                UsageBucket(start=b[0], end=b[1], label=b[2], success=success[i], failed=failed[i])
                for i, b in enumerate(buckets)
            ],
            range_success=sum(success),
            range_failed=sum(failed),
            per_voucher=by_use[:500],
            most_used=[r for r in by_use if r.in_range > 0][:5],
            least_used=sorted(rows, key=lambda r: (r.in_range, r.use_count, r.code))[:5],
        )

    # --- audit (read-only) -------------------------------------------
    def audit(
        self,
        *,
        q: str | None,
        action: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        offset: int,
    ) -> AuditPage:
        A = AuditLog
        query = select(A).where(A.entity_type.in_(AUDIT_ENTITIES))
        if action:
            query = query.where(A.action == action)
        if start is not None:
            query = query.where(A.occurred_at >= start)
        if end is not None:
            query = query.where(A.occurred_at < end)
        pattern = _like(q)
        if pattern:
            query = query.where(
                or_(
                    func.lower(A.action).like(pattern, escape="\\"),
                    func.lower(func.coalesce(A.entity_id, "")).like(pattern, escape="\\"),
                    func.lower(func.coalesce(cast(A.changes, String), "")).like(
                        pattern, escape="\\"
                    ),
                )
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(
            query.order_by(A.occurred_at.desc(), A.id.desc()).limit(limit).offset(offset)
        )
        items = []
        for a in rows:
            changes = dict(a.changes) if isinstance(a.changes, dict) else None
            via = changes.pop("via", None) if changes else None
            items.append(
                AuditEntryRead(
                    id=a.id,
                    occurred_at=a.occurred_at,
                    actor=a.actor.value if hasattr(a.actor, "value") else str(a.actor),
                    via=via,
                    action=a.action,
                    entity_type=a.entity_type,
                    entity_id=a.entity_id,
                    changes=changes or None,
                )
            )
        return AuditPage(items=items, total=total, limit=limit, offset=offset)
