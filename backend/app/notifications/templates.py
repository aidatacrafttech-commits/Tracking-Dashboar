"""Alert email rendering (plain text + HTML).

Templates receive only the explicit fields in ``AlertEmailContext``; no settings object,
headers or provider responses ever reach them. Every value is HTML-escaped, and the final
output is passed through the log redactor as a last line of defence against credentials.
"""

from __future__ import annotations

import html
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import redact_text

BRAND = "API Cost Monitor"

SOURCE_LABELS = {
    "api_verified": "Verified by the provider API",
    "manual": "Entered manually",
    "usage_estimate": "Estimated from usage (conservative)",
}

# Official account pages. Deep links only where verified; otherwise the console root.
RECOMMENDED_ACTIONS: dict[str, tuple[str, str]] = {
    "openai": (
        "Add credit in the OpenAI platform billing settings, then record the new balance in "
        "the dashboard (OpenAI has no balance API).",
        "https://platform.openai.com",
    ),
    "sarvam": (
        "Add credits in the Sarvam dashboard billing page, then record the new balance in "
        "the dashboard (Sarvam has no balance API).",
        "https://dashboard.sarvam.ai/billing",
    ),
    "deepgram": (
        "Top up the Deepgram project balance in the Deepgram console.",
        "https://console.deepgram.com",
    ),
    "elevenlabs": (
        "Upgrade the plan or enable usage-based billing in your ElevenLabs subscription "
        "settings before the credit quota runs out.",
        "https://elevenlabs.io",
    ),
    "aws": (
        "Review AWS credits and month-to-date spend in the Billing console; promotional "
        "credits will stop covering charges once exhausted.",
        "https://console.aws.amazon.com/billing/",
    ),
}


@dataclass
class AlertEmailContext:
    provider_slug: str
    provider_name: str
    event_type: str  # breached | reminder | recovered | test
    balance: Decimal | None
    threshold: Decimal
    unit: str
    recovery_boundary: Decimal
    source_type: str | None
    observed_at: datetime | None
    last_sync_at: datetime | None
    data_age_seconds: int | None
    display_timezone: str
    last_payment: dict[str, str] | None = None
    usage_summary: list[tuple[str, str]] = field(default_factory=list)
    conversion_note: str | None = None
    dashboard_url: str | None = None
    breach_episode: int | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Decimal):
                data[key] = str(value)
            elif isinstance(value, datetime):
                data[key] = value.isoformat()
        return data


def is_currency_unit(unit: str) -> bool:
    return len(unit) == 3 and unit.isalpha() and unit.isupper()


def format_amount(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "unknown"
    if is_currency_unit(unit):
        return f"{value:,.2f} {unit}"
    if unit == "CREDITS":
        return (
            f"{value:,.0f} credits" if value == value.to_integral_value() else f"{value:,} credits"
        )
    return f"{value:,} {unit}"


def format_time(moment: datetime | None, tz_name: str) -> str:
    if moment is None:
        return "never"
    utc = moment.strftime("%Y-%m-%d %H:%M UTC")
    if tz_name == "UTC":
        return utc
    local = moment.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")
    return f"{local} ({utc})"


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _subject(c: AlertEmailContext) -> str:
    bal = format_amount(c.balance, c.unit)
    thr = format_amount(c.threshold, c.unit)
    if c.event_type == "test":
        return f"[Test] {BRAND} email delivery check"
    if c.event_type == "recovered":
        return f"[Recovered] {c.provider_name} balance back above threshold: {bal}"
    prefix = "[Reminder] " if c.event_type == "reminder" else "[Low balance] "
    still = "still " if c.event_type == "reminder" else ""
    return f"{prefix}{c.provider_name} {still}at {bal} (threshold {thr})"


def _headline(c: AlertEmailContext) -> str:
    if c.event_type == "test":
        return "This is a test message. No balance has crossed a threshold."
    if c.event_type == "recovered":
        return (
            f"{c.provider_name} has recovered: the balance is above the recovery boundary "
            f"of {format_amount(c.recovery_boundary, c.unit)}."
        )
    if c.event_type == "reminder":
        return f"{c.provider_name} is still at or below its low-balance threshold."
    return f"{c.provider_name} has reached or dropped below its low-balance threshold."


def _rows(c: AlertEmailContext) -> list[tuple[str, str]]:
    rows = [
        ("Provider", c.provider_name),
        ("Current remaining balance", format_amount(c.balance, c.unit)),
        ("Threshold", format_amount(c.threshold, c.unit)),
        ("Recovery boundary", format_amount(c.recovery_boundary, c.unit)),
        ("Unit / currency", c.unit if is_currency_unit(c.unit) else f"{c.unit} (not money)"),
        ("Data source", SOURCE_LABELS.get(c.source_type or "", c.source_type or "unknown")),
        ("Balance observed", format_time(c.observed_at, c.display_timezone)),
        ("Data age", format_age(c.data_age_seconds)),
        ("Last successful sync", format_time(c.last_sync_at, c.display_timezone)),
    ]
    if c.conversion_note:
        rows.append(("Currency conversion", c.conversion_note))
    if c.last_payment:
        p = c.last_payment
        rows.append(("Last payment", f"{p['amount']} on {p['date']} ({p['method']})"))
    else:
        rows.append(("Last payment", "none recorded"))
    return rows


def render_email(c: AlertEmailContext) -> tuple[str, str, str]:
    """Return (subject, text, html)."""
    subject = _subject(c)
    action, action_url = RECOMMENDED_ACTIONS.get(
        c.provider_slug, ("Review the provider account.", "")
    )
    if c.event_type == "recovered":
        action = "No action needed. Keep monitoring."
    if c.event_type == "test":
        action = "If you received this, SMTP delivery is configured correctly."
        action_url = ""
    rows = [] if c.event_type == "test" else _rows(c)

    text_lines = [_headline(c), ""]
    text_lines += [f"{label}: {value}" for label, value in rows]
    usage_items = c.usage_summary or [("No usage recorded", "in the last 7 days")]
    if c.event_type != "test":
        text_lines += ["", "Recent usage (last 7 days):"]
        text_lines += [f"  - {label}: {value}" for label, value in usage_items]
    text_lines += ["", f"Recommended action: {action}"]
    if action_url:
        text_lines.append(f"Provider console: {action_url}")
    if c.dashboard_url:
        text_lines.append(f"Dashboard: {c.dashboard_url}")
    text_lines += ["", f"-- {BRAND}. Automated message; do not reply."]
    text = "\n".join(text_lines)

    e = html.escape
    accent = {
        "breached": "#b42318",
        "reminder": "#b54708",
        "recovered": "#067647",
        "test": "#175cd3",
    }.get(c.event_type, "#344054")
    table = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#475467;white-space:nowrap">{e(label)}</td>'
        f'<td style="padding:6px 0;color:#101828;font-weight:600">{e(value)}</td></tr>'
        for label, value in rows
    )
    usage = ""
    if c.event_type != "test":
        items = "".join(
            f"<li>{e(label)}: <strong>{e(value)}</strong></li>" for label, value in usage_items
        )
        usage = (
            f'<h3 style="font-size:14px;margin:20px 0 6px;color:#101828">'
            f'Recent usage (last 7 days)</h3><ul style="margin:0;padding-left:18px;'
            f'color:#344054">{items}</ul>'
        )
    links = ""
    if action_url:
        links += (
            f'<a href="{e(action_url)}" style="display:inline-block;margin-right:12px;'
            f'color:#175cd3">Open provider console</a>'
        )
    if c.dashboard_url:
        links += f'<a href="{e(c.dashboard_url)}" style="color:#175cd3">Open dashboard</a>'
    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f9fafb;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" style="max-width:560px;margin:0 auto;background:#ffffff;
border:1px solid #eaecf0;border-radius:8px">
<tr><td style="padding:20px 24px;border-top:4px solid {accent};border-radius:8px 8px 0 0">
<div style="font-size:12px;color:#667085;text-transform:uppercase;letter-spacing:.04em">
{e(BRAND)}</div>
<h2 style="margin:6px 0 4px;font-size:18px;color:#101828">{e(subject)}</h2>
<p style="margin:0 0 16px;color:#344054">{e(_headline(c))}</p>
<table role="presentation" style="border-collapse:collapse;font-size:14px">{table}</table>
{usage}
<h3 style="font-size:14px;margin:20px 0 6px;color:#101828">Recommended action</h3>
<p style="margin:0 0 12px;color:#344054">{e(action)}</p>
<p style="margin:0">{links}</p>
</td></tr>
<tr><td style="padding:12px 24px;border-top:1px solid #eaecf0;font-size:12px;color:#98a2b3">
Automated message from {e(BRAND)}. Do not reply.</td></tr>
</table></body></html>"""
    return redact_text(subject)[:255], redact_text(text), redact_text(html_body)
