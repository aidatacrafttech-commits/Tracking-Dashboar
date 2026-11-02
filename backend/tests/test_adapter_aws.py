"""AWS adapter tests with botocore Stubber.

Stubber validates every request's parameters against the real AWS service models shipped
with botocore, so these tests also prove we call real operations with valid arguments.
No network calls are made and no AWS charges are incurred.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import botocore.session
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from botocore.stub import ANY, Stubber

from app.models.enums import HealthStatus
from app.providers.aws import AwsAdapter, classify_aws_error
from app.providers.types import (
    DataLatency,
    ErrorCode,
    MetricType,
    OperationResult,
    SourceType,
    SyncStatus,
)

NOW = datetime(2026, 10, 12, 15, 30, tzinfo=UTC)
REGION = "ap-south-1"


class StubbedAws:
    def __init__(self) -> None:
        session = botocore.session.get_session()
        self.clients: dict[tuple[str, str], Any] = {}
        self.stubbers: dict[tuple[str, str], Stubber] = {}
        self._session = session

    def stub(self, service: str, region: str = "us-east-1") -> Stubber:
        key = (service, region)
        if key not in self.clients:
            client = self._session.create_client(
                service,
                region_name=region,
                aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
                aws_secret_access_key="fake-secret",  # noqa: S106
            )
            stubber = Stubber(client)
            stubber.activate()
            self.clients[key] = client
            self.stubbers[key] = stubber
        return self.stubbers[key]

    def factory(self, service: str, region: str) -> Any:
        self.stub(service, region)
        return self.clients[(service, region)]

    def assert_done(self) -> None:
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


def adapter(aws: StubbedAws, **kw: Any) -> AwsAdapter:
    return AwsAdapter(aws.factory, regions=[REGION], clock=lambda: NOW, **kw)


def run(coro) -> OperationResult:  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def caller_identity(aws: StubbedAws) -> None:
    aws.stub("sts").add_response(
        "get_caller_identity",
        {
            "UserId": "AIDAEXAMPLE",
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/m",
        },
        {},
    )


def credit(
    remaining: str, *, status: str = "ENABLED", end: datetime | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "creditId": f"c-{remaining}",
        "accountId": "123456789012",
        "creditType": "Promotion",
        "initialAmount": {"currencyCode": "USD", "currencyAmount": "1000"},
        "remainingAmount": {"currencyCode": "USD", "currencyAmount": remaining},
        "estimatedAmount": {"currencyCode": "USD", "currencyAmount": remaining},
        "description": "Activate credits",
        "startDate": NOW - timedelta(days=100),
        "creditStatus": status,
    }
    if end is not None:
        item["endDate"] = end
    return item


def test_credits_sum_active_only() -> None:
    aws = StubbedAws()
    caller_identity(aws)
    aws.stub("billing").add_response(
        "get_credits",
        {
            "credits": [
                credit("250.50", end=NOW + timedelta(days=30)),
                credit("100.25"),
                credit("999", end=NOW - timedelta(days=1)),  # expired
                credit("500", status="DISABLED"),
            ]
        },
        {"accountId": "123456789012", "startDate": ANY},
    )
    result = run(adapter(aws).fetch_balance())
    aws.assert_done()
    [metric] = result.metrics
    assert metric.metric_type is MetricType.PROMOTIONAL_CREDIT_REMAINING
    assert metric.value == Decimal("350.75")
    assert metric.latency is DataLatency.DELAYED
    assert metric.details is not None and metric.details["credit_count"] == 2


def test_no_credits_is_zero_remaining() -> None:
    aws = StubbedAws()
    caller_identity(aws)
    aws.stub("billing").add_response(
        "get_credits", {"credits": []}, {"accountId": ANY, "startDate": ANY}
    )
    [metric] = run(adapter(aws).fetch_balance()).metrics
    assert metric.value == Decimal(0)


def test_missing_billing_permission() -> None:
    aws = StubbedAws()
    caller_identity(aws)
    aws.stub("billing").add_client_error(
        "get_credits", service_error_code="AccessDeniedException", http_status_code=403
    )
    result = run(adapter(aws).fetch_balance())
    assert result.status is SyncStatus.ERROR
    assert result.error is not None and result.error.code is ErrorCode.PERMISSION_DENIED
    assert "GetCredits" in result.error.message


def ce_day(day: str, groups: list[tuple[str, str]], estimated: bool) -> dict[str, Any]:
    start = datetime.fromisoformat(day).date()
    return {
        "TimePeriod": {"Start": day, "End": (start + timedelta(days=1)).isoformat()},
        "Total": {},
        "Groups": [
            {"Keys": [svc], "Metrics": {"UnblendedCost": {"Amount": amt, "Unit": "USD"}}}
            for svc, amt in groups
        ],
        "Estimated": estimated,
    }


def test_cost_explorer_daily_by_service_with_pagination() -> None:
    aws = StubbedAws()
    ce = aws.stub("ce")
    expected = {
        "TimePeriod": {"Start": "2026-09-11", "End": "2026-10-13"},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    ce.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                ce_day("2026-10-10", [("Amazon S3", "1.1"), ("Amazon EC2", "2.2")], False)
            ],
            "NextPageToken": "tok",
        },
        expected,
    )
    ce.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": [ce_day("2026-10-11", [("Amazon EC2", "0.000001")], True)]},
        {**expected, "NextPageToken": "tok"},
    )
    result = run(adapter(aws).fetch_usage(datetime(2026, 9, 11, tzinfo=UTC), NOW))
    aws.assert_done()
    totals = [m for m in result.metrics if m.dimension == ""]
    assert [(m.period_start.day, m.value, m.is_final) for m in totals] == [  # type: ignore[union-attr]
        (10, Decimal("3.3"), True),
        (11, Decimal("0.000001"), False),  # Estimated -> not final
    ]
    s3 = next(m for m in result.metrics if m.dimension == "Amazon S3")
    assert s3.latency is DataLatency.DELAYED and s3.source_type is SourceType.API_VERIFIED


def test_billing_month_to_date_and_forecast() -> None:
    aws = StubbedAws()
    ce = aws.stub("ce")
    ce.add_response(
        "get_cost_and_usage",
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
                    "Total": {"UnblendedCost": {"Amount": "120.40", "Unit": "USD"}},
                    "Groups": [],
                    "Estimated": False,
                },
                {
                    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-13"},
                    "Total": {"UnblendedCost": {"Amount": "45.10", "Unit": "USD"}},
                    "Groups": [],
                    "Estimated": True,
                },
            ]
        },
        {
            "TimePeriod": {"Start": "2026-09-01", "End": "2026-10-13"},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
        },
    )
    ce.add_response(
        "get_cost_forecast",
        {"Total": {"Amount": "70.00", "Unit": "USD"}, "ForecastResultsByTime": []},
        {
            "TimePeriod": {"Start": "2026-10-12", "End": "2026-11-01"},
            "Metric": "UNBLENDED_COST",
            "Granularity": "MONTHLY",
        },
    )
    result = run(adapter(aws).fetch_billing_summary(NOW - timedelta(days=40), NOW))
    aws.assert_done()
    assert result.status is SyncStatus.OK
    months = [m for m in result.metrics if m.metric_type is MetricType.COST_MONTH]
    assert [(m.period_start.month, m.period_end.month, m.value) for m in months] == [  # type: ignore[union-attr]
        (9, 10, Decimal("120.40")),
        (10, 11, Decimal("45.10")),  # normalized to the calendar month
    ]
    [forecast] = [m for m in result.metrics if m.metric_type is MetricType.COST_FORECAST_MONTH]
    assert forecast.value == Decimal("115.10")  # month-to-date + forecast remainder
    assert forecast.source_type is SourceType.USAGE_ESTIMATE


def test_forecast_unavailable_is_partial_not_failure() -> None:
    aws = StubbedAws()
    ce = aws.stub("ce")
    ce.add_response("get_cost_and_usage", {"ResultsByTime": []}, None)
    ce.add_client_error("get_cost_forecast", service_error_code="DataUnavailableException")
    result = run(adapter(aws).fetch_billing_summary(NOW, NOW))
    assert result.status is SyncStatus.PARTIAL
    assert any("forecast unavailable" in w for w in result.warnings)


def test_cost_explorer_throttling() -> None:
    aws = StubbedAws()
    aws.stub("ce").add_client_error(
        "get_cost_and_usage", service_error_code="LimitExceededException", http_status_code=400
    )
    result = run(adapter(aws).fetch_usage(NOW - timedelta(days=2), NOW))
    assert result.error is not None and result.error.code is ErrorCode.RATE_LIMITED


def test_disabled_cost_explorer_makes_no_calls() -> None:
    aws = StubbedAws()
    result = run(adapter(aws, cost_explorer_enabled=False).fetch_usage(NOW, NOW))
    assert result.status is SyncStatus.DISABLED
    assert aws.clients == {}


def test_resources_ec2_ebs_and_s3_metrics() -> None:
    aws = StubbedAws()
    ec2 = aws.stub("ec2", REGION)
    ec2.add_response(
        "describe_instance_status",
        {
            "InstanceStatuses": [
                {
                    "InstanceId": "i-ok",
                    "InstanceState": {"Code": 16, "Name": "running"},
                    "InstanceStatus": {"Status": "ok"},
                    "SystemStatus": {"Status": "ok"},
                },
                {
                    "InstanceId": "i-bad",
                    "InstanceState": {"Code": 16, "Name": "running"},
                    "InstanceStatus": {"Status": "impaired"},
                    "SystemStatus": {"Status": "ok"},
                },
            ]
        },
        {"IncludeAllInstances": True},
    )
    ec2.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-ok",
                            "InstanceType": "t3.medium",
                            "State": {"Code": 16, "Name": "running"},
                            "Tags": [{"Key": "Name", "Value": "api-server"}],
                            "Placement": {"AvailabilityZone": "ap-south-1a"},
                        },
                        {
                            "InstanceId": "i-bad",
                            "InstanceType": "t3.small",
                            "State": {"Code": 16, "Name": "running"},
                        },
                        {
                            "InstanceId": "i-off",
                            "InstanceType": "t3.micro",
                            "State": {"Code": 80, "Name": "stopped"},
                        },
                    ]
                }
            ]
        },
        {},
    )
    ec2.add_response(
        "describe_volumes",
        {
            "Volumes": [
                {
                    "VolumeId": "vol-1",
                    "Size": 30,
                    "VolumeType": "gp3",
                    "State": "in-use",
                    "Encrypted": True,
                    "AvailabilityZone": "ap-south-1a",
                    "Attachments": [{"InstanceId": "i-ok", "VolumeId": "vol-1"}],
                }
            ]
        },
        {},
    )
    cw = aws.stub("cloudwatch", REGION)
    size_metric = {
        "Namespace": "AWS/S3",
        "MetricName": "BucketSizeBytes",
        "Dimensions": [
            {"Name": "BucketName", "Value": "media"},
            {"Name": "StorageType", "Value": "StandardStorage"},
        ],
    }
    cw.add_response(
        "list_metrics",
        {"Metrics": [size_metric]},
        {"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes"},
    )
    cw.add_response(
        "list_metrics", {"Metrics": []}, {"Namespace": "AWS/S3", "MetricName": "NumberOfObjects"}
    )
    cw.add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "m0",
                    "Label": "BucketSizeBytes",
                    "StatusCode": "Complete",
                    "Timestamps": [NOW - timedelta(days=2), NOW - timedelta(days=1)],
                    "Values": [1000.0, 2048.0],
                }
            ]
        },
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    result = run(adapter(aws).fetch_resources())
    aws.assert_done()
    assert result.status is SyncStatus.OK
    by_id = {r.resource_id: r for r in result.resources}
    assert by_id["i-ok"].health_status == "ok" and by_id["i-ok"].name == "api-server"
    assert by_id["i-bad"].health_status == "impaired"
    assert by_id["i-off"].state == "stopped" and by_id["i-off"].health_status is None
    assert by_id["vol-1"].details["size_gib"] == 30
    [s3] = result.metrics
    assert s3.metric_type is MetricType.S3_BUCKET_SIZE_GB
    assert s3.value == Decimal("0.000002048") and s3.unit == "GB"
    assert s3.dimension == "media/StandardStorage"


def test_resources_region_failure_is_isolated() -> None:
    aws = StubbedAws()
    aws.stub("ec2", REGION).add_client_error(
        "describe_instance_status", service_error_code="UnauthorizedOperation", http_status_code=403
    )
    result = run(adapter(aws).fetch_resources())
    assert result.status is SyncStatus.ERROR
    assert result.error is not None and result.error.code is ErrorCode.PERMISSION_DENIED


def test_health_degraded_when_instance_impaired() -> None:
    aws = StubbedAws()
    caller_identity(aws)
    aws.stub("ec2", REGION).add_response(
        "describe_instance_status",
        {
            "InstanceStatuses": [
                {
                    "InstanceId": "i-1",
                    "InstanceStatus": {"Status": "impaired"},
                    "SystemStatus": {"Status": "ok"},
                }
            ]
        },
        {},
    )
    result = run(adapter(aws).health_check())
    assert result.health is not None and result.health.status is HealthStatus.DEGRADED


def test_health_bad_credentials() -> None:
    aws = StubbedAws()
    aws.stub("sts").add_client_error(
        "get_caller_identity", service_error_code="InvalidClientTokenId", http_status_code=403
    )
    result = run(adapter(aws).health_check())
    assert result.error is not None and result.error.code is ErrorCode.AUTH_FAILED


def test_adapter_never_calls_mutating_operations() -> None:
    import inspect

    import app.providers.aws as module

    source = inspect.getsource(module)
    for verb in (
        "terminate_instances",
        "stop_instances",
        "delete_",
        "create_",
        "modify_",
        "run_instances",
        "put_",
    ):
        assert verb not in source, verb


def test_classify_sdk_errors() -> None:
    assert classify_aws_error(NoCredentialsError()).code is ErrorCode.NOT_CONFIGURED
    assert (
        classify_aws_error(EndpointConnectionError(endpoint_url="https://ce")).code
        is ErrorCode.NETWORK_ERROR
    )
