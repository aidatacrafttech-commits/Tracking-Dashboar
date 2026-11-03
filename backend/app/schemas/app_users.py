"""App Users screen: counts from the AdhyaapakAI database. Money is in integer paise."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import ApiModel


class AppUsersStatus(ApiModel):
    configured: bool
    database: str | None  # database name only (never the connection URL)


class DayRow(ApiModel):
    date: date
    signups: int  # accounts created that day that still exist
    deleted_since: int  # accounts created that day and permanently deleted since
    purchases: int  # paid course purchases (refunds and vouchers excluded)
    vouchers: int  # courses unlocked with a voucher
    revenue_paise: int  # INR purchases only


class Breakdown(ApiModel):
    label: str
    count: int


class TodayBlock(ApiModel):
    date: date
    signups: int
    signups_yesterday: int
    active_today: int
    active_7d: int
    active_30d: int
    purchases: int
    vouchers: int
    revenue_paise: int
    deletions_scheduled: int


class PeriodTotals(ApiModel):
    signups: int  # including accounts deleted since
    deleted_since: int
    average_per_day: float
    best_day: date | None
    best_day_signups: int
    with_course: int  # of this period's (remaining) signups, how many hold a course
    purchases: int
    vouchers: int
    revenue_paise: int
    other_currency_purchases: int  # App Store purchases not in INR (not in revenue)
    total_users: int  # all accounts in the app today


class AppUsersSummary(ApiModel):
    timezone: str
    start: date
    end: date
    days: int
    generated_at: datetime
    active_tracked_since: date
    today: TodayBlock
    totals: PeriodTotals
    daily: list[DayRow]
    by_class: list[Breakdown]
    by_stream: list[Breakdown]
    by_goal: list[Breakdown]


class SignupRow(ApiModel):
    signed_up_at: datetime
    name: str | None  # full name for the owner, first name only when masked
    class_level: int | None
    stream: str | None
    goals: list[str]
    email: str | None  # masked for viewers, e.g. a***@gmail.com
    phone: str | None  # masked for viewers, e.g. ******3210
    has_course: bool
    deletion_scheduled: bool


class SignupList(ApiModel):
    start: date
    end: date
    timezone: str
    masked: bool  # true for viewers: only the owner sees full contact details
    more: bool  # true when more people signed up than the list shows
    signups: list[SignupRow]


class DeletionItem(ApiModel):
    requested_at: datetime | None
    status: str | None  # scheduled, reactivated, in_progress or completed
    due_at: datetime | None  # when a scheduled deletion becomes permanent
    completed_at: datetime | None
    reactivated_at: datetime | None
    signed_up_at: datetime | None
    profile_exists: bool  # false once the account is permanently deleted
    name: str | None
    email: str | None  # masked for viewers
    phone: str | None  # masked for viewers


class DeletionList(ApiModel):
    start: date
    end: date
    timezone: str
    masked: bool
    more: bool
    deletions: list[DeletionItem]


class LiveNow(ApiModel):
    online_now: int  # distinct users in any signal below
    app_open: int | None  # app heartbeats; None until ADHYAAPAK_JWT_SECRET is set
    using_app: int  # any app request in the last `online_window_minutes`
    watching_video: int  # video progress saved in the last 25 seconds
    in_live_tutor: int  # Live Tutor heartbeat in the last 30 seconds
    asking_doubts: int  # doubt chat message in the last 5 minutes
    taking_test: int  # test finished in the last 5 minutes
    online_window_minutes: int
    checked_at: datetime
