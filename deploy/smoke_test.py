#!/usr/bin/env python3
"""Smoke test for a running API Cost Monitor. Standard library only.

Read-only by default: it never creates payments, thresholds or balances, never triggers a
provider sync and sends no email. The single exception is --send-test-email ADDRESS, an
explicit, controlled test that emails exactly that address.

    # health, readiness, security headers, unauthenticated access is refused
    python3 smoke_test.py --base-url https://monitor.example.com

    # also sign in (token read from $SMOKE_TOKEN or prompted; never printed) and check
    # providers, data sources, freshness, AWS separation and secret leakage
    python3 smoke_test.py --base-url https://monitor.example.com --login

    # what the systemd health-check timer runs
    python3 smoke_test.py --base-url http://127.0.0.1:8100 --ready-only --disk-path /var/lib/api-cost-monitor

Exit status 0 = every check passed (warnings allowed), 1 = at least one check failed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

EXPECTED_PROVIDERS = ("openai", "sarvam", "deepgram", "elevenlabs", "aws")
SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "AWS access key id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SECURITY_HEADERS = {
    "content-security-policy": None,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    raw_headers: list[tuple[str, str]]
    body: bytes

    def json(self):  # type: ignore[no-untyped-def]
        return json.loads(self.body or b"null")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass
class Client:
    base: str
    context: ssl.SSLContext | None
    cookie: str | None = None
    csrf: str | None = None
    secrets: list[str] = field(default_factory=list)

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        headers = {"Accept": "application/json, text/html", "User-Agent": "acm-smoke-test"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method not in ("GET", "HEAD"):
            headers["Origin"] = self.base  # same-origin write, as a browser would send
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20, context=self.context) as resp:  # noqa: S310
                return Response(
                    resp.status,
                    {k.lower(): v for k, v in resp.headers.items()},
                    list(resp.headers.items()),
                    resp.read(),
                )
        except urllib.error.HTTPError as err:
            return Response(
                err.code,
                {k.lower(): v for k, v in err.headers.items()},
                list(err.headers.items()),
                err.read(),
            )


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"PASS  {name}" + (f" — {detail}" if detail else ""))

    def warn(self, name: str, detail: str) -> None:
        print(f"WARN  {name} — {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"FAIL  {name} — {detail}", file=sys.stderr if os.environ.get("INVOCATION_ID") else sys.stdout)

    def check(self, name: str, condition: bool, detail: str = "", failure: str = "") -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, failure or detail or "check failed")
        return condition


def scan_for_secrets(report: Report, label: str, text: str, secrets: list[str]) -> bool:
    for secret in secrets:
        if secret and secret in text:
            report.fail(f"no secrets in {label}", "the dashboard token appears in the response")
            return False
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            report.fail(f"no secrets in {label}", f"found something that looks like an {name}")
            return False
    return True


def check_ready(client: Client, report: Report) -> None:
    health = client.request("GET", "/api/v1/health")
    report.check("liveness /api/v1/health", health.status == 200, f"HTTP {health.status}", f"HTTP {health.status}")
    ready = client.request("GET", "/api/v1/ready")
    try:
        body = ready.json()
    except ValueError:
        report.fail("readiness /api/v1/ready", f"HTTP {ready.status}, not JSON")
        return
    checks = body.get("checks", {}) if isinstance(body, dict) else {}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(checks.items()))
    if report.check("readiness /api/v1/ready", ready.status == 200 and body.get("status") == "ready", summary, f"HTTP {ready.status}: {summary}"):
        for name, value in sorted(checks.items()):
            if value == "warning":
                report.warn(f"readiness: {name}", "degraded (see /api/v1/admin/operations)")


def check_disk(report: Report, path: str, min_free_mb: int) -> None:
    free_mb = shutil.disk_usage(path).free // (1024 * 1024)
    report.check(f"free disk at {path}", free_mb >= min_free_mb, f"{free_mb} MB free", f"only {free_mb} MB free (minimum {min_free_mb} MB)")


def check_public_surface(client: Client, report: Report, expect_https: bool) -> None:
    page = client.request("GET", "/")
    if report.check("dashboard UI served at /", page.status == 200 and "<!doctype html" in page.text.lower(), f"HTTP {page.status}"):
        missing = [h for h, v in SECURITY_HEADERS.items() if h not in page.headers or (v and page.headers[h] != v)]
        report.check("security headers on the UI", not missing, "CSP, nosniff, DENY framing, no-referrer", f"missing or wrong: {', '.join(missing)}")
        csp = page.headers.get("content-security-policy", "")
        report.check("CSP forbids inline scripts", "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src", 1)[-1].split(";", 1)[0], "", csp)
        if expect_https:
            report.check("HSTS on the UI", "max-age=" in page.headers.get("strict-transport-security", ""), "", "Strict-Transport-Security header missing")
        # Follow the entry script's imports so lazily loaded chunks are scanned too.
        pending = set(re.findall(r'(?:src|href)="(/assets/[^"]+\.js)"', page.text))
        assets: set[str] = set()
        bundle = page.text
        while pending:
            asset = pending.pop()
            assets.add(asset)
            resp = client.request("GET", asset)
            if resp.status != 200 or "javascript" not in resp.headers.get("content-type", ""):
                continue  # not a real chunk (the UI fallback answers unknown paths with HTML)
            bundle += resp.text
            # Vite chunk names look like Charts-BgbMowqd.js (name-hash.js).
            found = re.findall(r"\b([A-Za-z0-9_]+-[A-Za-z0-9_-]{8}\.js)\b", resp.text)
            pending |= {f"/assets/{name}" for name in found} - assets
        if scan_for_secrets(report, "the frontend bundle", bundle, client.secrets):
            report.ok("no secrets in the frontend bundle", f"{len(assets)} script file(s) scanned")
    report.check("API refuses anonymous access", client.request("GET", "/api/v1/providers").status == 401, "GET /api/v1/providers -> 401")
    docs = client.request("GET", "/docs").status
    report.check("API docs disabled in production", docs == 404, f"/docs -> {docs}", f"/docs -> {docs} (set APP_ENV=production)")
    for probe in ("/.env", "/backend/.env", "/../backend/.env"):
        resp = client.request("GET", probe)
        leaked = resp.status == 200 and re.search(r"(?m)^[A-Z][A-Z0-9_]{2,}=", resp.text)
        report.check(f"no env file served at {probe}", not leaked, f"HTTP {resp.status}, not an env file", "response looks like an env file")


def login(client: Client, report: Report, token: str, expect_https: bool) -> bool:
    resp = client.request("POST", "/api/v1/auth/login", {"token": token, "remember": False})
    if resp.status != 200:
        report.fail("sign in", f"HTTP {resp.status} ({resp.json().get('error', {}).get('code') if resp.body else 'no body'})")
        return False
    cookies = [v for k, v in resp.raw_headers if k.lower() == "set-cookie"]
    session = next((c for c in cookies if c.split("=", 1)[0] in ("acm_session", "__Host-acm_session")), None)
    if session is None:
        report.fail("sign in", "no session cookie returned")
        return False
    attrs = session.lower()
    name = session.split("=", 1)[0]
    report.check("session cookie is HttpOnly + SameSite=Strict", "httponly" in attrs and "samesite=strict" in attrs, name)
    if expect_https:
        report.check("session cookie is Secure (__Host- prefix)", name.startswith("__Host-") and "; secure" in attrs, name, f"{name} without Secure")
    client.cookie = session.split(";", 1)[0]
    client.csrf = resp.json().get("csrf_token")
    scan_for_secrets(report, "the sign-in response", resp.text, client.secrets)
    report.ok("sign in", f"role={resp.json().get('role')}")
    return True


def check_signed_in(client: Client, report: Report, expected: tuple[str, ...]) -> None:
    providers = client.request("GET", "/api/v1/providers")
    if not report.check("provider list", providers.status == 200, "", f"HTTP {providers.status}"):
        return
    items = providers.json()
    ids = [p["id"] for p in items]
    report.check("all providers present", set(expected) <= set(ids), ", ".join(ids), f"missing: {sorted(set(expected) - set(ids))}")
    print("      provider     capability    integration      balance source       freshness")
    for p in items:
        bal = p.get("latest_balance")
        fresh = p.get("latest_balance_freshness") or {}
        source = (bal or {}).get("source_type") or "none yet"
        age = fresh.get("age_seconds")
        freshness = "—" if age is None else f"{age // 60} min old{' (STALE)' if fresh.get('is_stale') else ''}"
        status = (p.get("integration") or {}).get("status", "?")
        print(f"      {p['id']:<12} {p['balance_capability']:<13} {status:<16} {source:<20} {freshness}")
    unsupported: list[str] = []
    for pid in ids:
        sync = client.request("GET", f"/api/v1/providers/{pid}/sync-status")
        if sync.status != 200:
            report.fail(f"sync status for {pid}", f"HTTP {sync.status}")
            continue
        ops = sync.json()["operations"]
        unsupported += [f"{pid}:{o['operation']}" for o in ops if not o["supported"]]
        failing = [f"{o['operation']} ({o['error_code']})" for o in ops if o["status"] == "error"]
        if failing:
            report.warn(f"sync status for {pid}", "failing: " + ", ".join(failing))
    report.ok("unsupported operations are reported explicitly", ", ".join(unsupported) or "none")
    for path, label in (
        ("/api/v1/overview", "overview"),
        ("/api/v1/aws/resources", "AWS infrastructure (separate from billing)"),
        ("/api/v1/providers/aws/usage-series?days=7", "AWS cost series"),
        ("/api/v1/payments?limit=5", "payment history"),
        ("/api/v1/alerts?limit=5", "alert history"),
        ("/api/v1/notifications/status", "notification status"),
    ):
        resp = client.request("GET", path)
        if report.check(label, resp.status == 200, f"GET {path.split('?')[0]}", f"HTTP {resp.status}"):
            scan_for_secrets(report, label, resp.text, client.secrets)
    notify = client.request("GET", "/api/v1/notifications/status").json()
    report.check("SMTP password never returned", not any("password" in k and k != "smtp_auth_configured" for k in notify), "")
    print(f"      email delivery enabled: {notify.get('email_delivery_enabled')}, SMTP configured: {notify.get('smtp_configured')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="e.g. https://monitor.example.com or http://127.0.0.1:8100")
    parser.add_argument("--ready-only", action="store_true", help="only liveness, readiness and disk")
    parser.add_argument("--login", action="store_true", help="sign in with $SMOKE_TOKEN (or a prompt) and check data")
    parser.add_argument("--send-test-email", metavar="ADDRESS", help="controlled test: email exactly this address")
    parser.add_argument("--disk-path", help="also check free space on this path")
    parser.add_argument("--min-free-mb", type=int, default=1024)
    parser.add_argument("--ca-file", help="trust this CA/certificate (self-signed rehearsal)")
    parser.add_argument("--expect-providers", default=",".join(EXPECTED_PROVIDERS))
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    https = urlsplit(base).scheme == "https"
    context = ssl.create_default_context(cafile=args.ca_file) if https else None
    client = Client(base, context)
    report = Report()
    print(f"Smoke test against {base}")

    check_ready(client, report)
    if args.disk_path:
        check_disk(report, args.disk_path, args.min_free_mb)
    if not args.ready_only:
        check_public_surface(client, report, https)
        if args.login or args.send_test_email:
            token = os.environ.get("SMOKE_TOKEN") or getpass.getpass("Dashboard token (input hidden): ")
            client.secrets.append(token)
            if login(client, report, token, https):
                check_signed_in(client, report, tuple(p for p in args.expect_providers.split(",") if p))
                if args.send_test_email:
                    resp = client.request("POST", "/api/v1/notifications/test-email", {"to": args.send_test_email})
                    body = resp.json() if resp.body else {}
                    report.check(
                        f"controlled test email to {args.send_test_email}",
                        resp.status == 200 and body.get("delivery_status") == "sent",
                        "delivered to the SMTP server",
                        f"HTTP {resp.status}: {body.get('delivery_status') or body.get('error', {}).get('code')} {body.get('error_message') or ''}".strip(),
                    )
                out = client.request("POST", "/api/v1/auth/logout")
                report.check("sign out", out.status == 204, "", f"HTTP {out.status}")
                client.cookie = None
                report.check("session is gone after sign out", client.request("GET", "/api/v1/providers").status == 401)
    print(f"{'FAILED' if report.failed else 'OK'}: {report.failed} failing check(s)")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
