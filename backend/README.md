# API Cost Monitor — backend

A standalone FastAPI service for a single owner. It monitors credit and balance at OpenAI, Sarvam AI, Deepgram, ElevenLabs and AWS, and keeps payment history and alert thresholds.
It runs separately from `adhyaapak-ai` and never touches its database.

## Setup (Windows, Python 3.12)

```powershell
cd backend
py -3.12 -m venv venv
.\venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env      # then edit .env; never commit it
```

Generate the owner token and paste it into `.env` as `DASHBOARD_OWNER_TOKEN`:

```powershell
.\venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Database

SQLite file at `backend/data/monitor.db`, with schema managed by Alembic. Migrations need only `DATABASE_URL`; they do not need any secrets.

```powershell
.\venv\Scripts\python -m alembic upgrade head    # apply
.\venv\Scripts\python -m alembic current         # show revision
.\venv\Scripts\python -m alembic check           # models vs migrations drift
```

The app never migrates automatically. `/api/v1/ready` returns 503 until the schema is at head.

## Run

```powershell
.\venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100 --workers 1
```

Use **one worker**. SQLite and the Phase 2 in-process scheduler both assume a single process.
API docs are at http://127.0.0.1:8100/docs. They are disabled automatically when `APP_ENV=production`.

## Test and lint

```powershell
.\venv\Scripts\python -m pytest
.\venv\Scripts\ruff check app tests migrations
```

## Conventions

- **Layout:** `api/` (routes) → `services/` (rules and transactions) → `repositories/` (queries) → `models/` (ORM). `schemas/` holds the Pydantic I/O models and `core/` holds config, logging, errors and security.
- **Money:** stored as integer micro-units (`value × 10^6`); in the API it is always a decimal *string*, and JSON floats are rejected.
- **Time:** stored and returned as UTC (`...Z`). The display timezone is a setting (`GET/PUT /api/v1/settings`).
- **Unknown values:** stored as `NULL` with an `unavailable_reason`, never as `0`. Each value records its `source` (`provider_api`, `billing_report`, `usage_estimate` or `manual`) and when it was observed.
- **Secrets:** they live only in `.env` and are never stored in SQLite or returned by the API. Logs are redacted.

## Provider integrations (Phase 2)

Adapters live in `app/providers/`. They all implement `fetch_balance`, `fetch_usage`, `fetch_billing_summary` and `health_check`; AWS also implements `fetch_resources`.
Every call returns a normalized `OperationResult` and never raises.
An operation a provider cannot support returns `unsupported` with the reason. It never returns made-up values.

| Provider | Balance | Usage | Billing / payments | Health |
|---|---|---|---|---|
| OpenAI (Admin key) | ✗ no supported API → manual entry + usage estimate | ✓ tokens and requests per model per day | ✓ daily USD costs | status page |
| Sarvam AI | ✗ no API → manual entry | ✗ no API | ✗ no API | status page |
| Deepgram | ✓ actual USD balance | ✓ audio hours, requests, TTS chars, tokens per day | ✓ daily USD cost; purchases → provider-verified payments (promotional orders skipped) | status page |
| ElevenLabs | ✓ plan **credits** remaining (not money); PAYG USD balance manual only | ✓ daily credits (deprecated `character-stats` endpoint) | ✓ current overage, open invoices | status page |
| AWS (opt-in) | ✓ promotional credits remaining (~24h lag) | ✓ daily cost per service (Cost Explorer, delayed) | ✓ month-to-date, last month, forecast | STS probe + EC2 status checks |

AWS also records EC2 instance state and health, EBS volumes and S3 storage metrics. It only reads; it never creates, stops, resizes or deletes resources.
Least-privilege policy: [`docs/aws-monitoring-iam-policy.json`](../docs/aws-monitoring-iam-policy.json).

**Labels shown in the API**
- `source_type` is one of `api_verified`, `usage_estimate`, `manual` or `unavailable`.
- `data_latency` is `delayed` for billing pipelines such as Cost Explorer, credits and provider cost reports.

**Polling.** One background loop runs in the single uvicorn worker.
- A per-provider lock prevents overlapping runs.
- Each operation has its own interval, plus an overall time budget.
- Failing providers back off to 2×, 4× and then 8× the interval.
- A failed poll writes no data. The last good snapshot stays, and `GET /providers/{id}/sync-status` shows the error, the failure count and the age of the last success.

**Endpoints added**
- `GET /api/v1/providers/{id}/sync-status`
- `POST /api/v1/providers/{id}/sync`: owner only, limited to once a minute
- `POST /api/v1/providers/{id}/balance-entries`: owner only, manual balance
- `GET /api/v1/providers/{id}/health-checks`
- `GET /api/v1/aws/resources`

**Configuration** (see `.env.example`). A provider with missing variables reports `not_configured` with the variable *names*; nothing crashes. AWS is off until `AWS_MONITORING_ENABLED=true`. Provider variables can also come from other env files via `EXTRA_ENV_FILES`; `backend/.env` wins on conflicts.

## Threshold alerts and email (Phase 3)

Each provider has its own threshold, editable at runtime with `PUT /api/v1/providers/{id}/threshold`. Fields:
- `amount` and `unit`
- `is_enabled`
- `eligible_sources`: which balance sources may trigger an alert (`api_verified`, `manual`, `usage_estimate`)
- `monitoring_interval_minutes`
- `notification_emails`, which falls back to `ALERT_RECIPIENTS`
- `notify_on_breach`, `notify_on_recovery` and `recovery_margin`
- `cooldown_minutes`: optional repeat interval while still low

**How a threshold is evaluated.** This happens after every balance poll, after every manual balance entry, after every threshold save, and once a minute.
- **Alert:** the balance is at or below the threshold.
- **Recovery:** the balance rises above threshold + `recovery_margin`.
- **Never treated as zero:** missing data (`no_data`).
- **Not acted on:** data older than the provider's `stale_after_minutes` (`stale`).
- **Never compared:** different currencies with no configured `EXCHANGE_RATES` + `EXCHANGE_RATE_SOURCE` (`currency_mismatch`), and credits versus money (`incompatible_units`).
- **AWS:** cost figures are never compared with credit thresholds, because thresholds only read balance snapshots of their own `balance_kind`.

The result is visible as `last_evaluation_status` and `last_evaluation_message` on the threshold.

**No repeats.**
- A breach sends one email.
- More low-balance emails are sent only every `cooldown_minutes`, if set.
- A new breach email is sent only after a recovery and a fresh crossing.
- State lives in the database, so restarts change nothing.
- Each email is an `alert_events` row with a unique dedupe key (`t{threshold}:e{episode}:{type}`), created in the same transaction as the state change, under an optimistic version lock. Duplicates cannot be created, even by concurrent workers.

**Delivery (outbox).** The rendered email is stored with the event. The scheduler sends due events over SMTP and records each attempt in `alert_delivery_attempts`.
- **Temporary failures** (SMTP down, 4xx replies) retry with bounded backoff: `ALERT_RETRY_BASE_SECONDS`, doubling, capped at `ALERT_RETRY_MAX_SECONDS`. After `ALERT_MAX_DELIVERY_ATTEMPTS` the event becomes `failed`; it stays recorded and can be re-queued with `POST /api/v1/alerts/{id}/retry`.
- **Permanent failures** (5xx, refused recipients) fail immediately.
- **Crashes:** a crash mid-send is recovered through a lease, so delivery is at-least-once with a stable `Message-ID`.

**Safe by default.** `EMAIL_DELIVERY_ENABLED=false`: alerts are evaluated and recorded as `suppressed`, but nothing is emailed. Only `POST /api/v1/notifications/test-email` (owner) sends, and it sends a clearly labelled test message.

**Endpoints**
- `GET /api/v1/alerts`, `GET /api/v1/alerts/{id}`: rendered email plus attempts
- `POST /api/v1/alerts/{id}/retry`
- `GET /api/v1/providers/{id}/alerts`
- `GET /api/v1/providers/{id}/alert-preview?event_type=breached|reminder|recovered`: renders with current data, sends nothing
- `POST /api/v1/providers/{id}/threshold/evaluate`
- `GET /api/v1/notifications/status`: no credentials
- `POST /api/v1/notifications/test-email`

Sample renders are in [`docs/email-previews/`](../docs/email-previews/).
Email content never includes API keys, SMTP credentials or tokens: templates receive explicit fields only, and their output passes through the secret redactor.

**Gmail:** `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_SECURITY=starttls`, with an App Password (requires 2-Step Verification).

## Dashboard UI (Phase 4)

The React dashboard lives in [`../frontend`](../frontend/README.md). After `npm run build`, this backend serves it at `/`, on the same origin, under a strict UI Content-Security-Policy. API paths keep `default-src 'none'`.

**Endpoints added for the UI** (owner-only mutations; all audited)
- `GET /api/v1/auth/session`: the token's role
- `GET /api/v1/overview`: counts, per-currency AWS totals, trends, recent activity
- `GET /api/v1/providers/{id}/usage-series?days=&metric=&by_dimension=`
- `GET /api/v1/payments`: search, filters, sort, pagination, totals per currency
- `GET`, `PUT` and `DELETE /api/v1/payments/{id}`: edit and delete manual entries only
- `GET /api/v1/payments/{id}/reconcile-preview` and `POST /api/v1/payments/{id}/reconcile`: an explicit, previewed **Apply to balance**. It is refused if the balance changed since the preview, or if the provider's balance comes from its API.
- `GET /api/v1/events`: Server-Sent Events of changed topic names (no data)
- `threshold_headroom` on provider responses: balance minus threshold, computed server-side, only in the same unit

**Payments vs balances.** A payment is never added to a balance automatically. The OpenAI usage estimate no longer adds recorded top-ups either. Only a reconciliation, or a new manual balance entry, changes the balance baseline.

**Optional settings:** `FRONTEND_DIST_DIR` (default `../frontend/dist`), `SSE_CHECK_SECONDS` (5) and `SSE_MAX_CONNECTION_SECONDS` (300).

## Security and reliability (Phase 5)

The browser UI signs in with a dashboard token and receives an HttpOnly, SameSite=Strict session cookie. The session is server-side and CSRF-protected; bearer tokens remain for scripts.

Other protections:
- login and bearer rate limits
- a Trusted-Host check and request-size limit
- database lease locks for background jobs
- cancellation-safe shutdown
- verified daily backups
- disk and permission checks
- extended readiness checks

Owner endpoints:
- `GET /api/v1/admin/operations`
- `POST /api/v1/admin/backup`
- `POST /api/v1/admin/sessions/revoke-all`

See [`docs/SECURITY.md`](../docs/SECURITY.md) for the full list of controls, credential rotation, least-privilege access, the deployment checklist and remaining risks.

Run in production behind HTTPS with:

```powershell
.\venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100 --workers 1 --timeout-graceful-shutdown 15 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

## Deploying to EC2

See [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md): production layout, systemd service, optional nginx/HTTPS, backups, updates and rollback. The deploy kit is in [`deploy/`](../deploy/).
