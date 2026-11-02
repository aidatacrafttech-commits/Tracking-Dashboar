# Phase 6 test report: testing and operational verification

**Run date:** 2026-10-15

**Environment**
- Windows 11 (10.0.26200)
- Python 3.12.10
- Node 24.14.0
- Playwright 1.64.0 with the locally installed Google Chrome

**Scope:** the API Cost Monitor backend (`backend/`) and dashboard UI (`frontend/`).

**Outcome**
- Every automated suite passes, and no product defects were found in this phase.
- The dashboard is **not yet cleared for production use.** The release blockers below must be closed first. Most important is that no integration has been verified against a live provider account.

## 1. Commands and actual results

All commands were run on the date above. Results are copied from the console output.

| # | Command (working directory) | Result |
|---|---|---|
| 1 | `venv\Scripts\python -m pytest -q --cov=app --cov-branch --cov-report=term:skip-covered --cov-report=html:../reports/backend-coverage` (backend/) | **340 passed**, 0 failed, 1 warning (third-party deprecation, see §6) in 54.5 s |
| 2 | `venv\Scripts\python -m pytest -q -s tests/test_scenario_low_balance.py` (backend/) | **1 passed**; trace in §3 |
| 3 | `venv\Scripts\python -m ruff check .` and `ruff format --check app tests` (backend/) | All checks passed; 117 files already formatted |
| 4 | `npx vitest run --coverage` (frontend/) | **68 passed** in 5 files, 0 failed |
| 5 | `npx tsc --noEmit -p tsconfig.app.json` (frontend/) | No errors |
| 6 | `npx eslint .` (frontend/), including jsx-a11y | No findings |
| 7 | `npm run build` (frontend/) | Built successfully |
| 8 | `npx playwright test` (frontend/) | **17 passed, 7 skipped**, 0 failed in 42.4 s. The skips are by design (§2.3). |
| 9 | `venv\Scripts\python -m pip_audit -r requirements.txt -r requirements-dev.txt` (backend/) | No known vulnerabilities |
| 10 | `npm audit` (frontend/) | 0 vulnerabilities |
| 11 | Read-only check of the real database: `alembic_version` and `PRAGMA quick_check` on `backend/data/monitor.db`, opened with `mode=ro` | Revision `0005` (head); integrity `ok` |
| 12 | Search `frontend/dist` for fixture values, MSW or the test tokens; search the app code for test imports | 0 hits; production code contains no fixture data |

## 2. Coverage

**How to open the HTML reports:** run `npm run coverage` in `frontend/`, or command 1 for the backend. The reports are written to `reports/backend-coverage/index.html` and `reports/frontend-coverage/index.html`.

| Suite | Statements | Branches | Combined |
|---|---|---|---|
| Backend (`app/`) | 95.9% (5,143 / 5,365) | 86.4% (902 / 1,044) | **94%** |
| Frontend (`src/`, tests excluded) | 84.5% | 73.5% | Lines **87.1%**, functions 80.4% |

**Least-covered areas, and why**
- **Backend `ops/storage.py` (80%):** the uncovered lines are POSIX-only permission branches that cannot run on Windows.
- **Backend `providers/common.py` (74%):** defensive parsing helpers.
- **Frontend `AwsPage.tsx`:** the Cost Explorer chart branches are uncovered because jsdom cannot draw on a canvas. The charts are exercised in the Playwright screenshots instead.
- **Frontend `status.ts`:** rarely used health labels.

### 2.1 Backend: requirement → tests

| Requirement | Where it is tested |
|---|---|
| Database models, migrations, repositories | `test_migrations.py` (upgrade, downgrade, re-upgrade, constraints), `test_repositories.py`, `test_types_and_money.py` (exact money storage, UTC) |
| All API routes and authentication rules | `test_auth.py`, `test_security.py` (about 45 tests: bypass attempts, CSRF and Origin checks, rate limits, viewer writes, session expiry and rotation), plus the per-feature API tests |
| Every provider adapter, with mocked responses | `test_adapters_http.py` (OpenAI, Deepgram, ElevenLabs, Sarvam, status pages), `test_adapter_aws.py` (botocore Stubber), `test_http_client.py` (retries, timeouts, safe errors) |
| Balance-source classification | `test_adapters_http.py`, `test_polling.py` (API-verified, manual, usage estimate), `test_phase4_api.py` |
| Threshold evaluation and currency handling | `test_alert_evaluator.py` (at, below and above the threshold; recovery margin; stale; no data; currency mismatch; configured exchange rates; incompatible units), `test_thresholds_api.py` |
| Payment history calculations | `test_payments_api.py`, `test_phase4_api.py` (refunds subtract; totals per currency, never mixed; ranges; search; explicit reconciliation only) |
| Alert deduplication and cooldowns | `test_alert_evaluator.py`, `test_phase3_api.py`, and the scenario in §3 |
| SMTP retries and persistent delivery | `test_outbox.py` (leases, backoff, giving up, crash recovery), `test_phase3_api.py` (real SMTP to a local sink, outage then retry, restart without duplicates) |
| Polling concurrency and restart recovery | `test_polling.py` (overlapping polls skipped, interrupted runs recovered), `test_security.py` (database lease locks across processes, cancellation), `test_reliability_paths.py` (new) |
| Stale snapshots and failed monitoring runs | `test_polling.py` (failure keeps the last good value and never writes zero), `test_alert_evaluator.py` (stale data never alerts), and the scenario step 11 |

**New in Phase 6**
- `test_scenario_low_balance.py`: the operational scenario in §3.
- `test_reliability_paths.py`: 11 tests covering:
  - the alert cycle skipping while another process holds the lease
  - every alert-cycle step failing without stopping the next cycle
  - the scheduler loop surviving failing ticks
  - one provider's failing due-check not blocking the others
  - evaluator and adapter-close failures being contained
  - the provider registry building every adapter without network access, and reporting missing or disabled configuration
  - readiness reporting a failed database or migration check

### 2.2 Frontend: requirement → tests (Vitest + Testing Library + MSW)

| Requirement | Tests |
|---|---|
| Dashboard loading and error states | Loading skeleton announced to screen readers; safe error showing the request ID; *Try again* recovers; error inside the alert dialog |
| Provider cards and details | Balance, source, staleness, threshold and headroom on each card; an unknown balance shown as "Unknown", never zero; missing configuration named without its value; stale-balance warning; manual balance entry (validation, CSRF, server refusal); *Sync now* with a safe failure message |
| Threshold editing and validation | Every client rule matches the server's limits; server field errors appear next to the field; a 409 concurrent-edit message; read-only for viewers |
| Payment creation and history | Form validation; amounts sent as strings; per-currency totals; filters; delete confirmation; reconciliation preview; provider-verified rows read-only |
| Alert history | Delivery status; retry for failed alerts only (owner); attempt history; email rendered in a sandboxed `iframe` where scripts cannot run; reason shown for a suppressed alert; filters; empty state |
| Authentication and authorization | Sign-in, rejection, rate-limit message, expiry notice, sign-out, nothing stored in browser storage, viewer sees no mutating controls, server 403 shown, viewer never calls owner-only APIs, owner operations banner |
| Responsive layout and accessibility basics | axe-core scans of the overview, payments, provider list and provider detail. Playwright runs **every page** at desktop (1440 px), tablet (834 px) and mobile (390 px), checks there is no horizontal overflow, runs axe on each page, checks keyboard skip-link and dialog focus, and opens the mobile menu |
| AWS is read-only | The page has no start, stop, terminate, delete, resize or reboot controls, and makes no non-GET AWS requests |

### 2.3 Browser end-to-end (Playwright)

**Setup**
- `e2e/serve_e2e.py` runs the **real** backend on a throwaway SQLite file filled with clearly labelled fixture data.
- Polling is turned off, so no provider is contacted.
- The backend serves the production build.

**Tests:** 8 tests × 3 viewports = 24 runs.

**The 7 skips, all deliberate**
- The keyboard test runs on desktop and tablet only: 1 skip.
- Two flows that change data run once, on desktop: 4 skips.
- The mobile-menu test runs on mobile only: 2 skips.

## 3. End-to-end operational scenario

**Test:** `backend/tests/test_scenario_low_balance.py::test_low_balance_lifecycle_and_restart`

**What is real and what is simulated**
- **Example provider:** the INR provider slot (Sarvam AI), fed by a scripted test adapter. Sarvam has no balance API, so production gets its balance from manual entry.
- **Production code downstream of that feed:**
  - the polling coordinator, ingest and threshold evaluation
  - the transactional email outbox
  - the real SMTP protocol, delivering to a **local sink**: no real recipient, and the address is `@example.com`
  - the authenticated HTTP API and the scheduler's alert cycle
  - an application restart on the same database file
- **Not involved:** no provider is contacted, no credits are spent, and AWS is not touched.
- **Simulated time:** elapsed time (the cooldown and the staleness window) is simulated by moving stored timestamps back. This is exactly what wall-clock time would do.

**Configured policy**
- Threshold: ₹300.00
- Recovery margin: ₹50.00, so the alert recovers only above ₹350.00
- Cooldown: none, meaning one email per breach. Step 9 then sets a 60-minute reminder.

| Step | Action | Expected | Observed |
|---|---|---|---|
| 1 | Provider reports ₹1,000 | Balance stored as API-verified INR | `amount=1000, unit=INR` ✅ |
| 2 | Threshold set to ₹300 (API, owner) | Saved; evaluated straight away | 200 OK ✅ |
| 3 | Monitoring cycle at ₹1,000 | No alert | `state=ok, alerts=0, emails=0` ✅ |
| 4–5 | Falls to ₹500, then a cycle | No alert; headroom ₹200 above | `state=ok, headroom=200, alerts=0` ✅ |
| 6–7 | Falls to ₹300, which counts as low because it is at or below the threshold | Exactly one `breached` event, **queued** (pending); delivered on the next scheduler tick | One event, `pending` before the tick and `sent` after; 1 email with subject "Sarvam AI at 300.00 INR (threshold 300.00 INR)" ✅ |
| 8 | Three more sync and cycle rounds, plus a manual re-evaluation | Duplicates suppressed | Still 1 event and 1 email ✅ |
| 9a | Falls to ₹200, no cooldown | State stays breached; no second email | 1 event, 1 email ✅ |
| 9b | Policy changed to a 60-minute reminder; 61 minutes pass; two cycles | Exactly one reminder, then the cooldown restarts; the policy change does not reset the alert state | `['breached','reminder']`, 2 emails ✅ |
| — | ₹2,000 payment recorded manually | **Not** added to the balance automatically | Balance still ₹200; still breached ✅ |
| 10a | Rises to ₹320, inside the recovery margin | Still breached (prevents flapping) | No new event ✅ |
| 10b | Rises to ₹1,200, then two cycles | One `recovered` email; state ok; no duplicate | `['breached','reminder','recovered']`, 3 emails ✅ |
| 11 | Provider returns HTTP 503 | Last good balance kept, never zero; balance sync shows an error. Once the stale window passes: flagged stale on the provider, headroom and overview; evaluation `stale`; **no alert from stale data** | Balance ₹1,200 kept; `consecutive_failures=1`; `is_stale=true`; evaluation `stale`; still 3 events ✅ |
| 12 | Restart: a new app process on the same database | Alert state, alert history, payments and snapshots unchanged; nothing re-sent; readiness ok | All identical; 3 alerts, 3 emails, 1 payment; database and migrations ok ✅ |

Trace printed by the test:

```
1 balance: amount=1000, unit=INR
3 at 1000: state=ok, alerts=0, emails=0
5 at 500: state=ok, headroom=200, alerts=0
7 at 300: state=breached, events=['breached'], emails=1
8 repeat x4: events=1, emails=1
9a at 200, no cooldown: state=breached, events=1, emails=1
9b cooldown 60m elapsed: events=['breached', 'reminder'], emails=2
10 recovery: state=ok, events=['breached', 'reminder', 'recovered'], emails=3
11 provider failure: balance=1200, stale=True, evaluation=stale
12 restart: state=ok, alerts=3, emails=3, payments=1
```

## 4. Changes made in Phase 6

No dashboard features were added, and no regressions were found.

**Tests added**
- Backend: `tests/test_scenario_low_balance.py` and `tests/test_reliability_paths.py`, 12 tests in total. The backend suite grew from 328 to 340.
- Frontend: `src/pages/pages.test.tsx`, 25 tests. The frontend suite grew from 43 to 68.

**Coverage tooling**
- Backend: `pytest-cov==7.1.0` added to `requirements-dev.txt`.
- Frontend: `@vitest/coverage-v8@5.0.4`, with a `coverage` block in `vite.config.ts` and an `npm run coverage` script.
- `reports/` added to `.gitignore`.

**Dead code removed**
- Methods no code called: `PaymentRepository.exists_reference`, `PaymentRepository.list` and `SnapshotRepository.add_usage`.
- The service layer already does that work, so behaviour is unchanged.

## 5. Manual verification checklist

Complete these on the real deployment. None of them can be automated without real credentials or a real inbox.

**Live provider integrations** (§7, blocker 1)
- [ ] **OpenAI:** with `OPENAI_ADMIN_KEY` set, *Sync now* shows usage and costs that match the OpenAI usage page for the same days. The balance stays "manual", because OpenAI has no balance API.
- [ ] **Deepgram:** *Sync now* shows a balance equal to Deepgram Console → Billing. Purchases appear as provider-verified payments.
- [ ] **ElevenLabs:** plan credits used and the limit match the subscription page.
- [ ] **Sarvam:** enter the balance shown on dashboard.sarvam.ai/billing by hand, and check that it appears labelled "Entered manually".
- [ ] **AWS** (only if enabled; Cost Explorer costs $0.01 per request):
  - Month-to-date and forecast match the Billing console within one day.
  - The EC2 and EBS lists match the console.
  - The IAM policy is exactly `docs/aws-monitoring-iam-policy.json`.

**Email**
- [ ] With `EMAIL_DELIVERY_ENABLED=true`, use **Thresholds → Send test email** to send to your own address.
- [ ] Confirm it arrives, isn't in spam, and that the links point to `DASHBOARD_URL`.

**Alerts on real data**
- [ ] Set a threshold just above a real balance.
- [ ] Confirm one email arrives, and no second one on the next cycle.
- [ ] Raise the threshold back above the recovery boundary and confirm the recovery email.

**Restart**
- [ ] Stop the server (Ctrl+C or SIGTERM) and confirm the log ends with "application stopped" and no tracebacks.
- [ ] Start it again and check that `/api/v1/ready` reports `ready`.

**Backups**
- [ ] Run `POST /api/v1/admin/backup`, or wait for the daily run.
- [ ] Confirm a `backups/monitor-*.db` file appears.
- [ ] Copy it off the machine.
- [ ] Practise one restore into a scratch folder.

**Security**
- [ ] Run `scripts/harden_permissions.ps1`, then check that `/api/v1/admin/operations` reports no permission findings.
- [ ] Sign in as the viewer and confirm that nothing can be changed.

**Remote access** (only if the dashboard will be reachable beyond this machine)
- [ ] Put it behind HTTPS and set `APP_ENV=production` and `ALLOWED_HOSTS`.
- [ ] Confirm the browser shows the cookie as `__Host-acm_session` with `Secure`.

**Display**
- [ ] Check the timezone in the sidebar ("Times shown in …").
- [ ] Spot-check one alert time against your clock.

## 6. Known limitations

- **Staleness is based on age, not on failures.**
  - A failed poll shows straight away as *Failing* on the sync status, and the last good balance is kept.
  - The balance itself is flagged stale only once it is older than the provider's `stale_after_minutes`: 120 minutes for Deepgram and ElevenLabs, 1,440 minutes for OpenAI and Sarvam (manual entries), and 2,160 minutes for AWS (billing data lags).
  - This is by design, so that one transient failure doesn't hide a good value. It does mean an outage shorter than the window isn't flagged as stale.
- **Adapters are tested against documented example responses, not live accounts.** A field a provider renames will surface as a safe `malformed_response` sync error. It won't produce a wrong number, but it is only caught at runtime.
- **The scenario's INR feed is scripted.** The real Sarvam adapter supports health checks only, and its balances come from manual entry. That manual path is tested separately (`test_manual_balance_entry_triggers_alert` and the UI manual-entry tests).
- **Time-based behaviour is tested by moving timestamps,** not by long-running soak tests. There are no load or performance tests; the app is designed for a single owner.
- **Multi-process safety is limited.** It relies on SQLite lease locks and has been tested only with several app instances on one machine and one database file. The supported deployment is one process.
- **jsdom cannot draw on a canvas,** so the chart internals have low unit-test coverage. Charts are checked in Playwright screenshots (`frontend/e2e/screenshots/`) and their data tables are tested.
- **Third-party test warning:** `StarletteDeprecationWarning` (httpx in the test client). It does not affect production.
- **Not tested:** email deliverability (SPF/DKIM/spam placement) and MFA, which doesn't exist.

## 7. Release blockers

| # | Blocker | Why it blocks | How to close it |
|---|---|---|---|
| 1 | **No integration has been verified against a live provider account** | Provider API shapes and permissions have been tested only against documented examples. The dashboard's numbers can't be trusted for money decisions until they are compared with each provider's console. | Complete the live-provider items in the §5 checklist |
| 2 | **An AWS access key exposed in the `adhyaapak-ai` repo has not been rotated** (found in the Phase 0 audit) | It is a live credential in another codebase. | Deactivate and delete the key in IAM, then create a least-privilege key for monitoring (`docs/SECURITY.md`) |
| 3 | **Real email delivery has not been confirmed** | Low-balance alerts are the main purpose of the system. | Send a test email from the Thresholds page to your own inbox |
| 4 | *Conditional:* HTTPS, `APP_ENV=production` and `ALLOWED_HOSTS` | This is a blocker **only if** the dashboard is reachable from other machines. Over plain HTTP, the session cookie isn't `Secure`. | Follow the deployment checklist in `docs/SECURITY.md` |

**Not blocking:** off-machine backup copies are strongly recommended, and are listed in §5.

**When the system can be called production-ready:** once blockers 1–3 are closed, and blocker 4 too if the dashboard is exposed, with every suite above still passing.
