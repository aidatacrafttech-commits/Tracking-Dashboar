"""AWS adapter (boto3). Read-only: it never creates, stops, resizes or deletes anything.

Operations and the IAM actions they need (least privilege):
  balance    sts:GetCallerIdentity, billing:GetCredits        remaining promotional credits
  usage      ce:GetCostAndUsage                               daily cost by service
  billing    ce:GetCostAndUsage, ce:GetCostForecast           month-to-date, last month, forecast
  resources  ec2:DescribeInstances, ec2:DescribeInstanceStatus, ec2:DescribeVolumes,
             cloudwatch:ListMetrics, cloudwatch:GetMetricData (S3 daily storage metrics)
  health     sts:GetCallerIdentity, ec2:DescribeInstanceStatus

Cost Explorer data refreshes at least once every 24 hours and costs $0.01 per request; it
is billing data, not a live balance. S3 storage metrics are published once a day.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
)

from app.core.time import utcnow
from app.models.enums import HealthSource, HealthStatus
from app.providers.base import CapabilityInfo, ProviderAdapter, to_decimal
from app.providers.common import parse_date
from app.providers.types import (
    DataLatency,
    ErrorCode,
    HealthResult,
    Metric,
    MetricType,
    Operation,
    OperationResult,
    ProviderError,
    ProviderOperationError,
    ResourceRecord,
    SourceType,
    SyncStatus,
)

ClientFactory = Callable[[str, str], Any]  # (service, region) -> boto3 client

GLOBAL_REGION = "us-east-1"  # Cost Explorer and Billing endpoints live here
MAX_CE_PAGES = 10

_AUTH_CODES = {
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredToken",
    "ExpiredTokenException",
    "AuthFailure",
    "SignatureDoesNotMatch",
    "InvalidSignatureException",
}
_THROTTLE_CODES = {
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
    "LimitExceededException",
    "TooManyRequestsException",
}
_PERMISSION_CODES = {"UnauthorizedOperation", "UnauthorizedException", "OptInRequired"}


def classify_aws_error(exc: Exception) -> ProviderError:
    if isinstance(exc, NoCredentialsError | PartialCredentialsError | ProfileNotFound):
        return ProviderError(ErrorCode.NOT_CONFIGURED, "AWS credentials not found or incomplete")
    if isinstance(exc, ConnectTimeoutError | ReadTimeoutError):
        return ProviderError(ErrorCode.TIMEOUT, "AWS request timed out", retryable=True)
    if isinstance(exc, EndpointConnectionError):
        return ProviderError(ErrorCode.NETWORK_ERROR, "cannot reach AWS endpoint", retryable=True)
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "Unknown"))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        op = getattr(exc, "operation_name", "operation")
        if code in _AUTH_CODES:
            return ProviderError(
                ErrorCode.AUTH_FAILED, f"AWS rejected the credentials ({code})", http_status=status
            )
        if code.startswith("AccessDenied") or code in _PERMISSION_CODES:
            return ProviderError(
                ErrorCode.PERMISSION_DENIED,
                f"IAM permission missing for {op} ({code})",
                http_status=status,
            )
        if code in _THROTTLE_CODES:
            return ProviderError(
                ErrorCode.RATE_LIMITED,
                f"AWS throttled {op} ({code})",
                retryable=True,
                http_status=status,
            )
        if code == "DataUnavailableException":
            return ProviderError(
                ErrorCode.DATA_UNAVAILABLE, f"AWS has no data yet for {op}", http_status=status
            )
        if code in {"ValidationException", "InvalidParameterValue", "InvalidParameterCombination"}:
            return ProviderError(
                ErrorCode.INVALID_REQUEST,
                f"AWS rejected {op} parameters ({code})",
                http_status=status,
            )
        if isinstance(status, int) and status >= 500:
            return ProviderError(
                ErrorCode.UPSTREAM_ERROR,
                f"AWS service error in {op} ({code})",
                retryable=True,
                http_status=status,
            )
        return ProviderError(
            ErrorCode.UNEXPECTED, f"AWS error in {op} ({code})", http_status=status
        )
    if isinstance(exc, BotoCoreError):
        return ProviderError(ErrorCode.UNEXPECTED, f"AWS SDK error ({type(exc).__name__})")
    return ProviderError(ErrorCode.UNEXPECTED, f"unexpected error ({type(exc).__name__})")


def default_client_factory(
    *,
    profile: str | None,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None,
    timeout: float,
) -> ClientFactory:
    import boto3

    session = boto3.session.Session(
        profile_name=profile,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
    )
    config = Config(
        retries={"mode": "standard", "max_attempts": 3},  # bounded, with backoff + throttling
        connect_timeout=min(timeout, 5.0),
        read_timeout=timeout,
        user_agent_extra="api-cost-monitor",
    )
    cache: dict[tuple[str, str], Any] = {}

    def factory(service: str, region: str) -> Any:
        key = (service, region)
        if key not in cache:
            cache[key] = session.client(service, region_name=region, config=config)
        return cache[key]

    return factory


def _ce_date(value: date) -> str:
    return value.isoformat()


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _amount(metrics: dict[str, Any], key: str = "UnblendedCost") -> tuple[Decimal, str]:
    value = metrics[key]
    return to_decimal(value["Amount"], key), str(value["Unit"]).upper()


class AwsAdapter(ProviderAdapter):
    slug = "aws"
    capabilities = {
        Operation.BALANCE: CapabilityInfo(
            True, "Remaining promotional credits via Billing GetCredits (updates every ~24h)"
        ),
        Operation.USAGE: CapabilityInfo(
            True, "Daily cost by service via Cost Explorer (delayed billing data, $0.01/request)"
        ),
        Operation.BILLING: CapabilityInfo(
            True, "Month-to-date, previous month and forecast via Cost Explorer"
        ),
        Operation.RESOURCES: CapabilityInfo(
            True, "EC2 instance state/health, EBS volumes, S3 storage metrics (read-only)"
        ),
        Operation.HEALTH: CapabilityInfo(
            True,
            "Credential probe (STS) and EC2 status checks; the AWS Health API needs a "
            "Business support plan and is not used",
        ),
    }

    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        regions: list[str],
        cost_explorer_enabled: bool = True,
        credits_enabled: bool = True,
        resources_enabled: bool = True,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client_factory
        self.regions = regions
        self.cost_explorer_enabled = cost_explorer_enabled
        self.credits_enabled = credits_enabled
        self.resources_enabled = resources_enabled
        self._clock = clock

    async def _call(self, fn: Callable[[], Any]) -> Any:
        """Run blocking boto3 code in a thread; convert SDK errors to ProviderErrors."""

        def guarded() -> Any:
            try:
                return fn()
            except (ClientError, BotoCoreError) as exc:
                raise ProviderOperationError(classify_aws_error(exc)) from exc

        return await asyncio.to_thread(guarded)

    def _disabled(self, operation: Operation, flag: str) -> OperationResult:
        return self.result(
            operation,
            status=SyncStatus.DISABLED,
            error=ProviderError(ErrorCode.UNSUPPORTED, f"disabled by {flag}=false"),
        )

    # --- balance: promotional credits -------------------------------------------------
    async def _balance(self) -> OperationResult:
        if not self.credits_enabled:
            return self._disabled(Operation.BALANCE, "AWS_CREDITS_ENABLED")
        now = self._clock()

        def fetch() -> list[dict[str, Any]]:
            account = self._client("sts", GLOBAL_REGION).get_caller_identity()["Account"]
            response = self._client("billing", GLOBAL_REGION).get_credits(
                accountId=account, startDate=now - timedelta(days=364)
            )
            return list(response.get("credits", []))

        credits = await self._call(fetch)
        totals: dict[str, Decimal] = defaultdict(Decimal)
        active: list[dict[str, Any]] = []
        for credit in credits:
            end = credit.get("endDate")
            if credit.get("creditStatus", "ENABLED") != "ENABLED" or (
                end is not None and end <= now
            ):
                continue
            remaining = credit["remainingAmount"]
            currency = str(remaining["currencyCode"]).upper()
            totals[currency] += to_decimal(remaining["currencyAmount"], "remainingAmount")
            estimated = credit.get("estimatedAmount")
            active.append(
                {
                    "description": credit.get("description"),
                    "remaining": str(remaining["currencyAmount"]),
                    "estimated_remaining": str(estimated["currencyAmount"]) if estimated else None,
                    "currency": currency,
                    "expires": end.isoformat() if end else None,
                    "exhaust_date": credit["exhaustDate"].isoformat()
                    if credit.get("exhaustDate")
                    else None,
                }
            )
        warnings: list[str] = []
        if len(totals) > 1:
            warnings.append(
                f"credits in several currencies ({', '.join(sorted(totals))}); USD shown"
            )
        metric = Metric(
            self.slug,
            MetricType.PROMOTIONAL_CREDIT_REMAINING,
            totals.get("USD", Decimal(0)),  # no active credits reported -> 0 remaining
            "USD",
            SourceType.API_VERIFIED,
            now,
            latency=DataLatency.DELAYED,
            details={"active_credits": active, "credit_count": len(active)},
        )
        return self.result(
            Operation.BALANCE, observed_at=now, metrics=(metric,), warnings=tuple(warnings)
        )

    # --- Cost Explorer ---------------------------------------------------------------------
    def _cost_and_usage(self, **kwargs: Any) -> list[dict[str, Any]]:
        ce = self._client("ce", GLOBAL_REGION)
        results: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_CE_PAGES):
            response = ce.get_cost_and_usage(
                **kwargs, **({"NextPageToken": token} if token else {})
            )
            results.extend(response.get("ResultsByTime", []))
            token = response.get("NextPageToken")
            if not token:
                break
        return results

    async def _usage(self, start: datetime, end: datetime) -> OperationResult:
        if not self.cost_explorer_enabled:
            return self._disabled(Operation.USAGE, "AWS_COST_EXPLORER_ENABLED")
        now = self._clock()
        end_day = min(end.date(), now.date()) + timedelta(days=1)  # End is exclusive
        rows = await self._call(
            lambda: self._cost_and_usage(
                TimePeriod={"Start": _ce_date(start.date()), "End": _ce_date(end_day)},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        )
        metrics: list[Metric] = []
        for row in rows:
            p_start = parse_date(row["TimePeriod"]["Start"], "TimePeriod.Start")
            p_end = parse_date(row["TimePeriod"]["End"], "TimePeriod.End")
            final = not row.get("Estimated", True)
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for group in row.get("Groups", []):
                amount, unit = _amount(group["Metrics"])
                totals[unit] += amount
                metrics.append(
                    Metric(
                        self.slug,
                        MetricType.COST,
                        amount,
                        unit,
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_end,
                        dimension=group["Keys"][0][:128],
                        latency=DataLatency.DELAYED,
                        is_final=final,
                    )
                )
            for unit, total in (totals or {"USD": Decimal(0)}).items():
                metrics.append(
                    Metric(
                        self.slug,
                        MetricType.COST,
                        total,
                        unit,
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_end,
                        latency=DataLatency.DELAYED,
                        is_final=final,
                    )
                )
        return self.result(Operation.USAGE, observed_at=now, metrics=tuple(metrics))

    async def _billing(self, start: datetime, end: datetime) -> OperationResult:
        if not self.cost_explorer_enabled:
            return self._disabled(Operation.BILLING, "AWS_COST_EXPLORER_ENABLED")
        now = self._clock()
        today = now.date()
        this_month = _month_start(today)
        prev_month = _month_start(this_month - timedelta(days=1))
        next_month = _next_month(today)

        rows = await self._call(
            lambda: self._cost_and_usage(
                TimePeriod={
                    "Start": _ce_date(prev_month),
                    "End": _ce_date(today + timedelta(days=1)),
                },
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
        )
        metrics: list[Metric] = []
        month_to_date: tuple[Decimal, str] | None = None
        for row in rows:
            month = parse_date(row["TimePeriod"]["Start"], "TimePeriod.Start").date()
            amount, unit = _amount(row["Total"])
            if _month_start(month) == this_month:
                month_to_date = (amount, unit)
            metrics.append(
                # Normalized to calendar-month bounds so the row is updated, not duplicated.
                Metric(
                    self.slug,
                    MetricType.COST_MONTH,
                    amount,
                    unit,
                    SourceType.API_VERIFIED,
                    now,
                    period_start=_utc(_month_start(month)),
                    period_end=_utc(_next_month(month)),
                    latency=DataLatency.DELAYED,
                    is_final=not row.get("Estimated", True),
                    details={"month_to_date": _month_start(month) == this_month},
                )
            )

        warnings: list[str] = []
        try:
            forecast = await self._call(
                lambda: self._client("ce", GLOBAL_REGION).get_cost_forecast(
                    TimePeriod={"Start": _ce_date(today), "End": _ce_date(next_month)},
                    Metric="UNBLENDED_COST",
                    Granularity="MONTHLY",
                )
            )
        except ProviderOperationError as exc:
            # New accounts often lack enough history to forecast; the rest is still valid.
            warnings.append(f"forecast unavailable: {exc.error.message}")
        else:
            remaining, unit = _amount({"Total": forecast["Total"]}, "Total")
            mtd = month_to_date[0] if month_to_date and month_to_date[1] == unit else None
            if mtd is None:
                warnings.append("forecast recorded without month-to-date actuals")
            metrics.append(
                Metric(
                    self.slug,
                    MetricType.COST_FORECAST_MONTH,
                    remaining + (mtd or Decimal(0)),
                    unit,
                    SourceType.USAGE_ESTIMATE,
                    now,
                    period_start=_utc(this_month),
                    period_end=_utc(next_month),
                    latency=DataLatency.DELAYED,
                    details={
                        "forecast_remaining": str(remaining),
                        "month_to_date": str(mtd) if mtd is not None else None,
                        "source": "ce:GetCostForecast",
                    },
                )
            )
        return self.result(
            Operation.BILLING,
            status=SyncStatus.PARTIAL if warnings else SyncStatus.OK,
            observed_at=now,
            metrics=tuple(metrics),
            warnings=tuple(warnings),
        )

    # --- resources -------------------------------------------------------------------------
    def _region_resources(self, region: str) -> tuple[list[ResourceRecord], list[dict[str, Any]]]:
        ec2 = self._client("ec2", region)
        statuses: dict[str, dict[str, Any]] = {}
        for page in ec2.get_paginator("describe_instance_status").paginate(
            IncludeAllInstances=True
        ):
            for item in page.get("InstanceStatuses", []):
                statuses[item["InstanceId"]] = item

        records: list[ResourceRecord] = []
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    iid = inst["InstanceId"]
                    status = statuses.get(iid, {})
                    inst_check = status.get("InstanceStatus", {}).get("Status")
                    sys_check = status.get("SystemStatus", {}).get("Status")
                    if "impaired" in (inst_check, sys_check):
                        health = "impaired"
                    elif inst_check == "ok" and sys_check == "ok":
                        health = "ok"
                    else:
                        health = inst_check or sys_check
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    records.append(
                        ResourceRecord(
                            resource_type="ec2_instance",
                            resource_id=iid,
                            region=region,
                            name=tags.get("Name"),
                            state=inst.get("State", {}).get("Name"),
                            health_status=health,
                            details={
                                "instance_type": inst.get("InstanceType"),
                                "availability_zone": inst.get("Placement", {}).get(
                                    "AvailabilityZone"
                                ),
                                "launch_time": inst["LaunchTime"].isoformat()
                                if inst.get("LaunchTime")
                                else None,
                                "instance_status": inst_check,
                                "system_status": sys_check,
                                "scheduled_events": len(status.get("Events", [])),
                            },
                        )
                    )
        for page in ec2.get_paginator("describe_volumes").paginate():
            for vol in page.get("Volumes", []):
                tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
                records.append(
                    ResourceRecord(
                        resource_type="ebs_volume",
                        resource_id=vol["VolumeId"],
                        region=region,
                        name=tags.get("Name"),
                        state=vol.get("State"),
                        health_status=None,
                        details={
                            "size_gib": vol.get("Size"),
                            "volume_type": vol.get("VolumeType"),
                            "iops": vol.get("Iops"),
                            "encrypted": vol.get("Encrypted"),
                            "availability_zone": vol.get("AvailabilityZone"),
                            "attached_instances": [
                                a.get("InstanceId") for a in vol.get("Attachments", [])
                            ],
                        },
                    )
                )

        # S3 daily storage metrics published to CloudWatch in this region.
        cw = self._client("cloudwatch", region)
        queries: list[dict[str, Any]] = []
        for metric_name in ("BucketSizeBytes", "NumberOfObjects"):
            for page in cw.get_paginator("list_metrics").paginate(
                Namespace="AWS/S3", MetricName=metric_name
            ):
                for m in page.get("Metrics", []):
                    queries.append(
                        {
                            "Id": f"m{len(queries)}",
                            "MetricStat": {"Metric": m, "Period": 86400, "Stat": "Average"},
                            "ReturnData": True,
                        }
                    )
        datapoints: list[dict[str, Any]] = []
        end = self._clock()
        for i in range(0, len(queries), 500):
            batch = queries[i : i + 500]
            response = cw.get_metric_data(
                MetricDataQueries=batch, StartTime=end - timedelta(days=3), EndTime=end
            )
            by_id = {q["Id"]: q["MetricStat"]["Metric"] for q in batch}
            for result in response.get("MetricDataResults", []):
                if result.get("Timestamps") and result.get("Values"):
                    latest = max(zip(result["Timestamps"], result["Values"], strict=False))
                    datapoints.append(
                        {"metric": by_id[result["Id"]], "timestamp": latest[0], "value": latest[1]}
                    )
        return records, datapoints

    async def _resources(self) -> OperationResult:
        if not self.resources_enabled:
            return self._disabled(Operation.RESOURCES, "AWS_RESOURCES_ENABLED")
        now = self._clock()
        records: list[ResourceRecord] = []
        metrics: list[Metric] = []
        errors: list[ProviderError] = []
        for region in self.regions:
            try:
                region_records, points = await self._call(
                    lambda r=region: self._region_resources(r)
                )
            except ProviderOperationError as exc:
                errors.append(exc.error)
                continue
            records.extend(region_records)
            for point in points:
                metric = point["metric"]
                dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
                bucket = dims.get("BucketName", "unknown")
                storage = dims.get("StorageType", "")
                is_size = metric["MetricName"] == "BucketSizeBytes"
                stamp = point["timestamp"].astimezone(UTC)
                p_start = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
                raw = to_decimal(point["value"], "s3 metric")
                # Bytes are stored as decimal GB: multi-TB buckets would overflow storage.
                value = raw / Decimal(10**9) if is_size else raw.to_integral_value()
                metrics.append(
                    Metric(
                        self.slug,
                        MetricType.S3_BUCKET_SIZE_GB if is_size else MetricType.S3_OBJECT_COUNT,
                        value,
                        "GB" if is_size else "objects",
                        SourceType.API_VERIFIED,
                        now,
                        period_start=p_start,
                        period_end=p_start + timedelta(days=1),
                        dimension=f"{bucket}/{storage}"[:128],
                        data_as_of=stamp,
                        latency=DataLatency.DELAYED,
                        is_final=True,
                        details={"region": region},
                    )
                )
        if errors and len(errors) == len(self.regions):
            return self.failure(Operation.RESOURCES, errors[0])
        return self.result(
            Operation.RESOURCES,
            status=SyncStatus.PARTIAL if errors else SyncStatus.OK,
            observed_at=now,
            resources=tuple(records),
            metrics=tuple(metrics),
            warnings=tuple(f"{e.code.value}: {e.message}" for e in errors),
        )

    # --- health ----------------------------------------------------------------------------
    async def _health(self) -> OperationResult:
        started = time.perf_counter()
        await self._call(lambda: self._client("sts", GLOBAL_REGION).get_caller_identity())
        latency = int((time.perf_counter() - started) * 1000)
        impaired = 0
        checked = 0
        warnings: list[str] = []
        if self.resources_enabled:
            for region in self.regions:
                try:
                    statuses = await self._call(
                        lambda r=region: self._client("ec2", r).describe_instance_status()[
                            "InstanceStatuses"
                        ]
                    )
                except ProviderOperationError as exc:
                    warnings.append(f"{region}: {exc.error.message}")
                    continue
                for item in statuses:
                    checked += 1
                    checks = (
                        item.get("InstanceStatus", {}).get("Status"),
                        item.get("SystemStatus", {}).get("Status"),
                    )
                    impaired += "impaired" in checks
        status = HealthStatus.DEGRADED if impaired else HealthStatus.OPERATIONAL
        return self.result(
            Operation.HEALTH,
            health=status_health(status, checked, impaired, latency),
            warnings=tuple(warnings),
        )


def status_health(
    status: HealthStatus, checked: int, impaired: int, latency_ms: int
) -> HealthResult:
    return HealthResult(
        status=status,
        source=HealthSource.API_PROBE,
        indicator="impaired" if impaired else "ok",
        description=f"credentials valid; {checked} instance(s) checked; {impaired} impaired",
        latency_ms=latency_ms,
    )
