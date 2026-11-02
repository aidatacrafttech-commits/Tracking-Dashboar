"""Verify live provider connections with the keys in backend/.env (read-only calls).

    venv\\Scripts\\python scripts\\verify_providers.py  # all four
    venv\\Scripts\\python scripts\\verify_providers.py --providers deepgram
    venv\\Scripts\\python scripts\\verify_providers.py --dry-run  # call the APIs, store nothing

Runs exactly what the dashboard's "Sync now" runs (same adapters, same storage), so the
results then appear in the dashboard. Every call only reads data; nothing is bought or
changed at any provider and no credits are consumed. AWS is excluded unless named
explicitly (Cost Explorer is billed per request).

Never prints keys: only configuration status, balances, counts and safe error summaries.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, register_secrets  # noqa: E402
from app.db.migrations import current_revision, head_revision  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.models.enums import RunTrigger  # noqa: E402
from app.providers.registry import build_runtimes  # noqa: E402
from app.providers.types import Metric, MetricType, Operation, OperationResult  # noqa: E402
from app.services.polling import PollingCoordinator  # noqa: E402

PRIORITY_1 = ("openai", "deepgram", "elevenlabs", "sarvam")
BALANCE_TYPES = {
    MetricType.PREPAID_BALANCE,
    MetricType.PLAN_QUOTA_REMAINING,
    MetricType.PROMOTIONAL_CREDIT_REMAINING,
}
SOURCE_LABEL = {
    "api_verified": "verified by provider API",
    "billing_report": "provider billing report (delayed)",
    "manual": "entered manually",
    "usage_estimate": "estimated from usage",
    "unavailable": "unavailable",
}


def fmt(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "unknown"
    return f"{value:,.2f} {unit}" if len(unit) == 3 and unit.isupper() else f"{value:,} {unit}"


def describe(result: OperationResult) -> list[str]:
    op = result.operation.value
    if result.error is not None:
        lines = [f"  {op:<9} FAILED  {result.error.code.value}: {result.error.message}"]
        if result.error.http_status:
            lines[0] += f" (HTTP {result.error.http_status})"
        return lines
    if result.status.value == "unsupported":
        return [f"  {op:<9} not available from this provider's API"]
    lines = [f"  {op:<9} {result.status.value.upper()}"]
    metrics: list[Metric] = list(result.metrics)
    for m in metrics:
        if m.metric_type in BALANCE_TYPES:
            note = f" - {m.unavailable_reason}" if m.unavailable_reason else ""
            lines.append(
                f"            {m.metric_type.value}: {fmt(m.value, m.unit)} "
                f"[{SOURCE_LABEL.get(m.source_type.value, m.source_type.value)}]{note}"
            )
    others = [m for m in metrics if m.metric_type not in BALANCE_TYPES]
    if others:
        totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        starts = [m.period_start for m in others if m.period_start]
        for m in others:
            if m.value is not None and m.dimension == "":
                totals[(m.metric_type.value, m.unit)] += m.value
        span = f"{min(starts):%Y-%m-%d} to {max(starts):%Y-%m-%d}" if starts else "current"
        lines.append(f"            {len(others)} usage/cost records ({span})")
        for (metric, unit), total in sorted(totals.items()):
            lines.append(f"            total {metric}: {fmt(total, unit)}")
    if result.payments:
        lines.append(f"            {len(result.payments)} provider-reported payment(s)")
    if result.health is not None:
        h = result.health
        lines.append(
            f"            service status: {h.status.value} ({h.description or h.indicator or '-'})"
        )
    for warning in result.warnings:
        lines.append(f"            note: {warning}")
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--providers", default=",".join(PRIORITY_1))
    parser.add_argument("--dry-run", action="store_true", help="call the APIs but store nothing")
    args = parser.parse_args()
    wanted = [p.strip() for p in args.providers.split(",") if p.strip()]

    settings = get_settings()
    configure_logging("ERROR", json_logs=False)  # keep the report readable
    register_secrets(*settings.secret_values())
    runtimes = build_runtimes(settings)
    unknown = [p for p in wanted if p not in runtimes]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}")
        return 2

    engine = create_db_engine(settings.resolved_database_url)
    if not args.dry_run and current_revision(engine) != head_revision(
        settings.resolved_database_url
    ):
        print("database is not migrated; run: venv\\Scripts\\python -m alembic upgrade head")
        return 2
    coordinator = PollingCoordinator(create_session_factory(engine), runtimes, settings)

    failures = 0
    for slug in wanted:
        runtime = runtimes[slug]
        print(f"\n=== {slug}")
        if runtime.status.value == "not_configured":
            print(f"  not configured - set in backend/.env: {', '.join(runtime.missing_config)}")
            failures += 1
            continue
        if runtime.status.value == "disabled":
            print(f"  disabled - {runtime.note}")
            continue
        unsupported = [op.value for op, info in runtime.capabilities.items() if not info.supported]
        if unsupported:
            print(f"  no API for: {', '.join(unsupported)} (enter these manually in the dashboard)")
        if args.dry_run:
            adapter = runtime.adapter
            if adapter is None:
                continue
            results = []
            for op in runtime.capabilities:
                if not runtime.capabilities[op].supported:
                    continue
                if op is Operation.BALANCE:
                    results.append(await adapter.fetch_balance())
                elif op is Operation.HEALTH:
                    results.append(await adapter.health_check())
                elif op is Operation.RESOURCES:
                    results.append(await adapter.fetch_resources())
                elif op is Operation.USAGE:
                    results.append(await adapter.fetch_usage(*coordinator._windows(op)))
                else:
                    results.append(await adapter.fetch_billing_summary(*coordinator._windows(op)))
        else:
            summary = await coordinator.run_provider(slug, trigger=RunTrigger.MANUAL)
            results = summary.results
            outcome = summary.status.value if summary.status else summary.reason
            print(f"  run #{summary.run_id}: {outcome}, {summary.records_written} record(s) stored")
        for result in results:
            for line in describe(result):
                print(line)
            failures += result.error is not None
    await coordinator.aclose()
    engine.dispose()
    done = (
        f"{failures} problem(s) - see above." if failures else "All configured providers responded."
    )
    print(f"\n{done}")
    if not args.dry_run:
        print("Results are stored; open the dashboard to see them.")
    return 1 if failures else 0


if __name__ == "__main__":
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    sys.exit(asyncio.run(main()))
