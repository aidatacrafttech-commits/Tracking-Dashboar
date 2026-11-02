"""Importing this package registers every table on ``Base.metadata`` (used by Alembic)."""

from app.models.alerting import AlertDeliveryAttempt, AlertEvent, ThresholdConfig
from app.models.course import (
    Chapter,
    Course,
    CourseAsset,
    CourseEnrollment,
    CourseTaxonomyTerm,
    EnrollmentEvent,
    Lecture,
    LectureResource,
    LectureVariant,
    Playlist,
)
from app.models.operations import ApplicationSetting, AuditLog, MonitoringRun, ServiceHealthCheck
from app.models.payment import PaymentTransaction
from app.models.provider import Provider
from app.models.security import AuthAttempt, AuthSession, JobLock
from app.models.snapshot import ProviderBalanceSnapshot, ProviderUsageSnapshot
from app.models.sync import InfrastructureResourceSnapshot, ProviderSyncState
from app.models.voucher import Voucher, VoucherTarget, VoucherUsageEvent, VoucherUser

__all__ = [
    "AlertDeliveryAttempt",
    "AlertEvent",
    "ApplicationSetting",
    "AuditLog",
    "AuthAttempt",
    "AuthSession",
    "Chapter",
    "Course",
    "CourseAsset",
    "CourseEnrollment",
    "CourseTaxonomyTerm",
    "EnrollmentEvent",
    "InfrastructureResourceSnapshot",
    "JobLock",
    "Lecture",
    "LectureResource",
    "LectureVariant",
    "MonitoringRun",
    "PaymentTransaction",
    "Playlist",
    "Provider",
    "ProviderBalanceSnapshot",
    "ProviderSyncState",
    "ProviderUsageSnapshot",
    "ServiceHealthCheck",
    "ThresholdConfig",
    "Voucher",
    "VoucherTarget",
    "VoucherUsageEvent",
    "VoucherUser",
]
