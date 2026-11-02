# Production-readiness audit

**Date:** 2026-10-15

**Scope:** backend, frontend, database and migrations, background jobs, email, authentication, deployment kit.

**Method:** inspected the code and configuration, ran every suite that is safe to run, and probed the running system. Earlier completion claims were treated as unverified until re-checked.

**Verdict: not production-ready.** The code passes its audit checks, and the three defects found here are fixed and covered by tests. However:
- No provider integration and no real email has ever run.
- The app has not been deployed to EC2.

Blockers are listed in §6.

## 1. Results

| # | Requirement | Status | Evidence | Risk | Remediation |
|---|---|---|---|---|---|
| 1 | Authentication on every private endpoint | PASS | New `test_route_auth_audit.py` enumerates all 39 operations from the app. Every non-public one returns 401 when anonymous. A viewer gets 403 on all 13 writes (session and bearer) and on admin reads. An owner cookie without a CSRF token gets 403. | Low | None. The test fails if a future route is added unprotected |
| 2 | CSRF / Origin protection | PASS | Unit tests, plus a live probe through nginx: a foreign Origin gets 403 `origin_not_allowed`; a missing or wrong token gets 403 `csrf_failed`; nothing was written | Low | — |
| 3 | Session management | PASS | HttpOnly, Secure, SameSite=Strict `__Host-` cookie, checked in Chrome through HTTPS and the tunnel; idle and absolute expiry, rotation and revoke-all have tests; no browser storage | Low | No MFA (accepted for a single owner) |
| 4 | No secrets in the frontend bundle, API responses or logs | PASS | The real `.env` values were searched across every file, the release archives, the coverage reports and the real database: 0 hits. 1,785 rehearsal log lines contain 0 tokens, cookies or key patterns. The smoke test scans 12 bundle files and every API response | Low | — |
| 5 | `.env` and `.gitignore` | **FIXED** | `git check-ignore`: `.env` files were ignored and the examples tracked. **Defect:** `*.db.partial` (an interrupted backup holds the full database) and key files (`*.pem`, `id_rsa`) were not ignored | Medium | Added `backend/backups/`, `*.partial`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`. The project is not a git repository yet, so there is no history to check |
| 6 | Least-privilege AWS IAM | PASS | The policy grants 9 read-only actions with no wildcards. The 9 operations `aws.py` uses (direct calls and paginators) are exactly these, and the code contains no mutating AWS call | Low | Resource `*` is required by these actions |
| 7 | Dependency vulnerabilities | PASS | `pip-audit` on the production `requirements.lock` and on the dev requirements: none. `npm audit` and `npm audit --omit=dev`: 0 | Low | Optional: hash-pin the lock (`--require-hashes`) |
| 8 | Safe errors, input validation, audit logs | PASS | Security and validation tests pass. The rehearsal's audit log recorded `auth.login`, `auth.login_failed` (5), `auth.logout`, `payment.create`, `threshold.create`, `balance.manual_entry`, `ops.backup` | Low | — |
| 9 | Unknown balance never treated as zero | **FIXED** | **Defect (high):** a Deepgram response with the `balances` field missing, null or reshaped was stored as a **provider-verified $0.00**, enough to trigger a false low-balance email. Reproduced with 4 payload shapes | High | `deepgram.py` now requires the field to be a list; only an explicit `[]` means $0. Regression test `test_deepgram_malformed_balance_is_unknown_not_zero` (5 cases). Other adapters checked: ElevenLabs already strict; AWS follows boto3's empty-list semantics; the other defaults affect only spend or payments |
| 10 | Incompatible currencies never compared | PASS | Tests: currency mismatch refused without a documented rate; credits never compared with money; AWS cost never compared with a credit threshold; headroom only computed in the same unit; payment totals kept per currency | Low | — |
| 11 | Threshold: above / equal / below | PASS | `test_above_threshold_no_alert`, `test_equal_to_threshold_breaches`, `test_below_threshold_creates_one_breach_alert`, plus the 12-step INR scenario | Low | — |
| 12 | Repeated polling, duplicate suppression | PASS | `test_repeated_polling_does_not_repeat_alert`, `test_duplicate_alert_creation_is_impossible` (unique dedupe key), scenario step 8 | Low | — |
| 13 | Recovery and a new crossing | PASS | `test_recovery_with_hysteresis_and_new_episode` (episode 2 gets a new breach alert), scenario step 10 | Low | — |
| 14 | Stale data and provider failure | PASS | `test_stale_data_is_not_acted_on`, `test_stale_data_cannot_recover_a_breach`, `test_failure_keeps_last_good_snapshot_and_marks_error`, scenario step 11, and the Deepgram fix (#9) | Low | — |
| 15 | Email failure and retry | PASS (mocked) | `test_transient_failure_retries_with_backoff`, `test_gives_up_after_max_attempts_but_keeps_event`, `test_permanent_failure_is_not_retried`, real SMTP protocol to a local sink (outage, auth failure, rejection) | Medium | Real SMTP not tested (blocker 2) |
| 16 | Restart with pending alerts | PASS | `test_restart_does_not_duplicate_alerts` (pending, restart, sent exactly once), `test_claim_is_exclusive_and_lease_recovers_crashes`, scenario step 12 | Low | — |
| 17 | Migrations | PASS | `alembic check`: no drift between models and migrations. The migration tests cover upgrade, downgrade and re-upgrade. The real database is at 0005 | Low | 5 migration files aren't in ruff's format (cosmetic; left unchanged) |
| 18 | Persistence, transactions, concurrency | PASS | Polling, outbox and lease-lock tests. Rehearsal: data intact across restart, simulated reboot, 9 deploys and 3 rollbacks (1 manual, 2 automatic) | Low | — |
| 19 | Backup and restore | PASS (rehearsal) | `manage.sh backup` → change → `restore` removed the later data and kept the earlier data. App backup restore gives a clean status. Regression test `test_restored_backup_is_healthy_and_unlocked` | Medium | Off-server copies are manual |
| 20 | Retries and timeouts | PASS | HTTP: ≤60 s timeout, ≤5 retries, `Retry-After` honoured, no redirects (`test_http_client.py`). AWS: standard mode, 3 attempts. SMTP timeout. Per-operation time budget | Low | — |
| 21 | Logging | **FIXED** | **Defect (low):** every request was logged twice (`app.access` + `uvicorn.access`), because the app's logging setup re-enabled uvicorn's access logger despite `--no-access-log`. Confirmed in the rehearsal journal | Low | `logging.py` silences `uvicorn.access`. Test `test_each_request_is_logged_once` fails without the fix. After deploying: 3 requests produced only `app.access` lines |
| 22 | Health checks | PASS (rehearsal) | `/api/v1/health`, `/api/v1/ready`; the timer ran 13 times with 0 failures | Low | — |
| 23 | HTTPS / reverse proxy | PASS (rehearsal) | nginx 1.24 with the shipped site: `nginx -t` ok, redirect, HSTS, Secure cookie, throttle (429 after 5 failures), bad Host gets 400; Chrome works | Medium | Real certificate and DNS not tested |
| 24 | Service startup, log rotation, persistent storage | PASS (rehearsal) / NOT TESTED (EC2) | User-level systemd unit and `systemd-analyze verify` pass. The root-only sandbox options, the `apimon` user and the journal namespace have not run anywhere | Medium | Verify on EC2 (`systemd-analyze security api-cost-monitor`) |
| 25 | Live EC2 deployment | BLOCKED | No SSH key or AWS access on this PC; a probe of the production host was blocked | High | You run `docs/DEPLOYMENT.md` §3–6 |
| 26 | Live provider integrations | BLOCKED | The real `backend/.env` has **only `DASHBOARD_OWNER_TOKEN`**; no provider keys, AWS off. The real database has 0 sync runs and 0 snapshots | **Critical** | Add read-only keys, then follow §4 |
| 27 | Real email delivery | BLOCKED | No SMTP in `.env`; `EMAIL_DELIVERY_ENABLED=false`. Only a local sink has received mail | High | §4, step 6 |

## 2. Providers

None of the five providers has ever synced on the real installation. "Last successful sync" is **never** for all five.

| Provider | Balance | Usage / billing | Payment history | Credentials and permissions | Value shown | Live tested |
|---|---|---|---|---|---|---|
| OpenAI | **No API.** Manual entry, optionally with an *estimate* (manual minus reported costs) | Usage `/v1/organization/usage/completions`; costs `/v1/organization/costs` (delayed) | Manual entry only | `OPENAI_ADMIN_KEY` (Admin key) | Balance: manual or estimated; spend: API-verified, delayed | **No** |
| Sarvam AI | **No API.** Manual entry | None (status page only) | Manual entry only | None (status page is public) | Manual, or "unavailable" | Status page: rehearsal only |
| Deepgram | `/v1/projects/{id}/balances` (actual USD) | `/usage/breakdown`, `/billing/breakdown` | **Provider-verified** from `/purchases` (paid orders; promotional ones skipped) | `DEEPGRAM_API_KEY` (billing:read, usage:read), `DEEPGRAM_PROJECT_ID` | API-verified | **No** |
| ElevenLabs | Plan credit quota `/v1/user/subscription` (credits, **not money**); the pay-as-you-go USD balance has no API, so it is manual | Daily credits from `/v1/usage/character-stats` (deprecated by ElevenLabs); overage and open invoices | Manual entry only (invoices are not payments) | `ELEVENLABS_API_KEY` | Quota: API-verified credits; USD: manual | **No** |
| AWS | Promotional credits via Billing `GetCredits` (about 24 h lag) | Cost Explorer: month-to-date, forecast, daily per service (USD 0.01 per request); EC2/EBS/S3 health shown separately | Manual entry only | Instance role or IAM user with `docs/aws-monitoring-iam-policy.json`; `AWS_MONITORING_ENABLED=true` | Credits: API-verified (delayed); costs: billing data, provisional | **No** |

**Error handling (mocked tests, per adapter):**
- authentication failure (401) and missing permission (403)
- rate limiting (429 with `Retry-After`)
- 5xx errors with bounded backoff
- timeouts and network errors
- malformed responses
- no redirects followed

When a sync fails, the last good value is kept, the failure is recorded on the sync status and shown in the UI, and no zero is written. The error message never contains the response body or credentials.

## 3. Commands and results (2026-10-15)

| Command | Result |
|---|---|
| Route audit script, then `pytest tests/test_route_auth_audit.py` | 39 operations, 0 unprotected; 6 passed |
| Secret-value scan (names only; the `.env` values are never printed) | Only `DASHBOARD_OWNER_TOKEN` is set; 0 files contain a secret; real database clean |
| Read-only query of `backend/data/monitor.db` | Revision 0005; 5 providers; every other table empty |
| `pytest -q --cov=app --cov-branch` (Windows) | **353 passed**, 94% |
| Same suite on Linux with `requirements.lock` (WSL, Python 3.12.3) | **353 passed** |
| `ruff check .` | Clean |
| `vitest run --coverage`, `tsc`, `eslint`, `npm run build` | 68 passed (87% lines); clean; clean; built |
| `playwright test` | 17 passed, 7 skipped (by design) |
| `pip_audit -r requirements.lock --disable-pip --no-deps`, `pip_audit -r requirements-dev.txt`, `npm audit` | No known vulnerabilities / 0 |
| `alembic upgrade head` + `alembic check` on a scratch database | No new upgrade operations detected |
| `git check-ignore` matrix (scratch repository) | 2 gaps found, fixed and re-verified |
| Deepgram adapter probe with 5 payload shapes | 4 produced a verified $0 (defect); after the fix: malformed_response |
| Rehearsal: deploy of the audit build, smoke test `--login`, acceptance verify, journal scans, timer history | All pass; 0 secrets in 1,785 log lines; duplicate access lines gone; timer 13 runs, 0 failures |

## 4. Manual checklist: real providers and email

Run these after adding real, read-only credentials to the server's `monitor.env` (or to `backend/.env` for a local check). Each step only reads data, except step 6, which sends one email to an address you choose.

1. **Deepgram.** Sign in, open Providers → Deepgram → *Sync now*. Then check:
   - The balance equals Console → Billing, is labelled "Verified by provider API", and shows a fresh timestamp.
   - Purchases appear in Payments as provider-verified.
   - A key without `billing:read` shows "permission denied" and no balance.
2. **OpenAI.** *Sync now*. Then check:
   - Usage and cost per day match platform.openai.com/usage for the same days. Cost is delayed by about a day.
   - The balance shows "No balance API: manual entry".
   - Enter the balance shown on the billing page and check it's labelled "Entered manually".
3. **ElevenLabs.** *Sync now*. Then check:
   - Credits used and the limit match the Subscription page, shown in credits, not money.
   - Overage or invoices appear under billing.
4. **Sarvam.** Enter the balance from dashboard.sarvam.ai/billing. It should show as "Entered manually", and *Service status* should show the public status page.
5. **AWS.** Set `AWS_MONITORING_ENABLED=true` (each Cost Explorer request costs USD 0.01). *Sync now*. Then check:
   - Month-to-date and forecast match the Billing console, allowing about a day of lag.
   - The EC2 and EBS lists match the console.
   - With a role lacking `billing:GetCredits`, credits show as "permission denied", not 0.
6. **Email (controlled).** Set the SMTP values and `ALERT_RECIPIENTS=<your address>`, keeping `EMAIL_DELIVERY_ENABLED=false`. Use Thresholds → *Send test email* to send to your own address; it shows the delivery error if SMTP is wrong. Then set `EMAIL_DELIVERY_ENABLED=true` and restart. Then check that:
   - one test email arrives, not in spam, with a working dashboard link
   - Alerts shows it as "Sent"
7. **Real alert path.** Set a threshold just **above** one real balance:
   - Exactly one low-balance email arrives.
   - *Check now* again: no second email.
   - Lower the threshold below the balance by more than the recovery margin: one recovery email.
8. **Failure path.** Temporarily put a wrong key for one provider and click *Sync now*. Check:
   - "Failing (auth_failed)" is shown, the last balance is kept, and no email is sent.
   - Then restore the key.
9. **Restart.** `sudo systemctl restart api-cost-monitor`, then check that balances, thresholds, payments and alerts are unchanged and nothing was re-sent.

## 5. Files changed during the audit

| File | Change |
|---|---|
| `backend/app/providers/deepgram.py` | A missing or malformed `balances` field is now an error, never $0 |
| `backend/app/core/logging.py` | `uvicorn.access` no longer duplicates the app's access log |
| `backend/tests/test_route_auth_audit.py` | New: authentication, role and CSRF checks on every operation |
| `backend/tests/test_adapters_http.py` | +5 cases: malformed Deepgram balances |
| `backend/tests/test_logging.py` | +1: each request is logged once |
| `.gitignore` | Backups, partial backup files, key files |
| `docs/PRODUCTION_AUDIT.md` | This report |

## 6. Remaining production blockers

1. **Critical: no live provider verification.** Add read-only keys and complete §4, steps 1–5.
2. **High: real email not verified.** Complete §4, steps 6–7.
3. **High: not deployed to EC2.** Follow `docs/DEPLOYMENT.md` and run the smoke test on the host. The systemd sandboxing, the `apimon` user, the journal namespace, the real certificate and the instance role have not run anywhere yet.
4. **High (outside this repository): the AWS access key exposed in `adhyaapak-ai`** (Phase 0) is unverified as rotated. Deactivate it and replace it with least-privilege credentials.

**Accepted risks:** single process with SQLite; no MFA; static tokens (single owner); manual off-server backup copies; dependency lock without hashes.

## 7. Live verification with real credentials (2026-10-15, local PC)

The keys and SMTP settings were added to `backend/.env` by the owner. No value was printed. Every call below is read-only.

| Provider | Status | Authenticated | Retrieved | Freshness | Kind of value |
|---|---|---|---|---|---|
| OpenAI | **PASS** | Yes (Admin key) | Usage: 452 daily records (tokens, requests by model); costs: 44 daily records, USD 1,140.04 for 2026-09-01 to 2026-10-14 | Usage up to today; costs to yesterday (delayed) | Usage and spend are API-verified. **No balance API**: enter it manually (an estimate can be derived) |
| Deepgram | **FAIL** | Yes: `GET /v1/projects` returns 200, and the configured project ID is the key's only project | Nothing: balances, usage, billing and project details all return **403 permission_denied** | — | The key's role lacks billing and usage read access. Create an **Owner/Admin** key for that project |
| ElevenLabs | **PASS** | Yes | Plan quota: 188,990 credits remaining; usage: 32 daily records; billing: open invoice INR 1,000.00, overage INR 0.00 | Realtime | Credit quota (**not money**), API-verified. The pay-as-you-go balance has no API and is entered manually |
| Sarvam AI | **PASS** (status page) | Not needed | Public status page: operational | Every 10 minutes | **No balance, usage or billing API**: manual entry |
| AWS | **NOT CONFIGURED** | — | — | — | Priority 2: `AWS_MONITORING_ENABLED=false`, no credentials |

**Email**
- Gmail SMTP (smtp.gmail.com:587, STARTTLS, TLS 1.3) is reachable.
- One test email was sent through `POST /api/v1/notifications/test-email`, only to the configured `ALERT_RECIPIENTS`. **Gmail accepted it** (`sent`, 1 attempt). Arrival in the inbox has to be confirmed by the recipient.
- `EMAIL_DELIVERY_ENABLED` is still `false`, so no real low-balance email is sent.
- `DASHBOARD_URL=http://localhost:8100` works only on this PC; links in emails won't open on a phone.

**Defect found and fixed**
- **Medium: newly added credentials were ignored for a full polling interval.** After keys are added and the app restarted, the "not configured" state left by the previous process made the scheduler wait a full interval (30 minutes; 6 hours for AWS Cost Explorer) before the first real poll, so the dashboard kept saying "not configured".
- **Fix:** `polling.py` and `ingest.py` now poll immediately when the last recorded state was not_configured or disabled and the provider now has an adapter.
- **Regression test:** `test_newly_configured_provider_is_polled_immediately`.
- **Live check:** after the restart, all three providers were polled within seconds.

**Tests after the fix:** 354 backend tests passed. The mocked threshold, email and restart tests (58) passed, and the INR scenario trace is unchanged.

## 8. Sarvam AI capability check (2026-10-15)

Sources: docs.sarvam.ai/api/platform/billing, docs.sarvam.ai/api/platform/faq, and the full API reference index (docs.sarvam.ai/api-reference/llms.txt, about 120 endpoints). The OpenAPI files that the index links to (`openapi.json`, `openapi.yaml`) return 404.

Live test with the owner's Sarvam key, read-only:
- `GET /voices` returns 200, so the key is valid.
- `GET /usage` (mentioned only by a third-party site) returns **404 not_found_error**.

| Metric | Status | Official location | Dashboard behaviour |
|---|---|---|---|
| Organisation remaining prepaid credits | **UNSUPPORTED** | Dashboard only: Settings → Billing ("Credits Left"; one balance per organisation) | Entered manually (INR, 1 credit = ₹1); stale after 24 h; threshold alerts work on it |
| Usage and spend by billing period | **UNSUPPORTED** | Dashboard only: Settings → Usage | Shown as "Not reported by provider" |
| Workspace-level usage | **UNSUPPORTED** | Dashboard only: Settings → Usage, attributed per workspace (no workspace budgets) | Not tracked |
| Payment history | **UNSUPPORTED** | Dashboard only: Settings → Billing → Payment History | Entered manually on the Payments page |
| Service status | **PASS** | Public status page (status.sarvam.ai) | Polled every 10 minutes; no key used |

No private browser endpoints, session cookies or page scraping are used. The dashboard is also behind a Cloudflare bot challenge.

`SARVAM_API_KEY` is not needed, because no documented endpoint uses it. The app never reads it (`test_sarvam_key_in_env_is_ignored`). The owner should remove it from `backend/.env` and delete the test key in the Sarvam dashboard.

**Tests:** `tests/test_sarvam_manual.py`, 6 tests:
- only the status page is contacted, and no key is sent
- the capability notes name the official dashboard locations
- a rate-limited status page gives a safe error
- a Sarvam key in the environment is ignored
- a manual INR balance triggers one breach alert with no duplicate, then one recovery
- a stale manual balance never alerts

The backend suite passes with 361 tests.
