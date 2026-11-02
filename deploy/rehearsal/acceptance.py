#!/usr/bin/env python3
"""Rehearsal-only acceptance steps. They WRITE data (thresholds, a manual balance, payments),
so they must never be pointed at the production dashboard. The token comes from $SMOKE_TOKEN.

    acceptance.py URL seed              set independent thresholds, a manual balance, a payment
    acceptance.py URL verify            check all of that survived (restart / reboot / restore)
    acceptance.py URL add-payment REF   record a marker payment (restore test)
    acceptance.py URL has-payment REF   exit 0 if a payment with REF exists, 1 if not
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_test import Client  # noqa: E402

THRESHOLDS = {
    "deepgram": {"amount": "10.00", "unit": "USD", "recovery_margin": "2.00"},
    "sarvam": {"amount": "500.00", "unit": "INR", "recovery_margin": "50.00", "cooldown_minutes": 60},
}


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"PASS  {msg}")


def signed_in(url: str) -> Client:
    import ssl

    ca = os.environ.get("SMOKE_CA_FILE")
    context = ssl.create_default_context(cafile=ca) if urlsplit(url).scheme == "https" else None
    client = Client(url.rstrip("/"), context)
    resp = client.request("POST", "/api/v1/auth/login", {"token": os.environ["SMOKE_TOKEN"], "remember": False})
    if resp.status != 200:
        fail(f"sign in: HTTP {resp.status}")
    cookie = next(v for k, v in resp.raw_headers if k.lower() == "set-cookie" and "acm_session=" in v)
    client.cookie = cookie.split(";", 1)[0]
    client.csrf = resp.json()["csrf_token"]
    return client


def payment(client: Client, ref: str, amount: str = "2000.00") -> None:
    resp = client.request(
        "POST",
        "/api/v1/providers/sarvam/payments",
        {"currency": "INR", "amount": amount, "transaction_date": "2026-10-15T10:00:00+05:30", "payment_reference": ref},
    )
    if resp.status != 201:
        fail(f"record payment {ref}: HTTP {resp.status} {resp.text[:200]}")
    ok(f"payment {ref} recorded ({amount} INR)")


def find_payment(client: Client, ref: str) -> list[dict]:
    return client.request("GET", f"/api/v1/payments?q={quote(ref)}").json()["items"]


def seed(client: Client) -> None:
    for pid, body in THRESHOLDS.items():
        resp = client.request("PUT", f"/api/v1/providers/{pid}/threshold", body)
        if resp.status != 200:
            fail(f"threshold for {pid}: HTTP {resp.status} {resp.text[:200]}")
    ok("thresholds set: deepgram 10.00 USD, sarvam 500.00 INR (cooldown 60 min)")
    resp = client.request("POST", "/api/v1/providers/sarvam/balance-entries", {"amount": "1000.00", "unit": "INR", "notes": "rehearsal"})
    if resp.status != 201:
        fail(f"manual balance: HTTP {resp.status} {resp.text[:200]}")
    ok("manual Sarvam balance recorded: 1000.00 INR")
    payment(client, "REHEARSAL-PAY-1")
    verify(client)


def verify(client: Client) -> None:
    for pid, body in THRESHOLDS.items():
        t = client.request("GET", f"/api/v1/providers/{pid}/threshold").json()
        same = Decimal(t["amount"]) == Decimal(body["amount"]) and t["unit"] == body["unit"]
        same = same and t.get("cooldown_minutes") == body.get("cooldown_minutes")
        if not same:
            fail(f"threshold for {pid} changed: {t['amount']} {t['unit']}")
    ok("thresholds intact and independent (deepgram USD, sarvam INR)")
    sarvam = client.request("GET", "/api/v1/providers/sarvam").json()
    bal = sarvam["latest_balance"]
    if not bal or Decimal(bal["amount"]) != 1000 or bal["source_type"] != "manual":
        fail(f"Sarvam balance: {bal}")
    ok(f"Sarvam balance 1000 INR, source=manual, age {sarvam['latest_balance_freshness']['age_seconds']} s")
    if not find_payment(client, "REHEARSAL-PAY-1"):
        fail("payment REHEARSAL-PAY-1 missing")
    ok("payment history persisted (REHEARSAL-PAY-1)")
    caps = {
        p["id"]: [c["operation"] for c in (p.get("integration") or {}).get("capabilities", []) if not c["supported"]]
        for p in client.request("GET", "/api/v1/providers").json()
    }
    if "balance" not in caps.get("sarvam", []):
        fail("Sarvam balance API should be reported as unsupported")
    ok(f"unsupported operations shown: sarvam={caps['sarvam']}")


def main() -> int:
    url, command, *rest = sys.argv[1:]
    client = signed_in(url)
    if command == "seed":
        seed(client)
    elif command == "verify":
        verify(client)
    elif command == "add-payment":
        payment(client, rest[0], "1.00")
    elif command == "has-payment":
        found = bool(find_payment(client, rest[0]))
        print(f"payment {rest[0]}: {'present' if found else 'absent'}")
        return 0 if found else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
