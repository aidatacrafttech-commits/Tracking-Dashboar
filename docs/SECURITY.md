# Security and operations guide

This guide covers the API Cost Monitor, a single-owner dashboard that holds financial data and provider credentials.

## What is protected, and how

**Authentication.** Every `/api/v1` route except `/health` and `/ready` requires authentication.

| Mechanism | Used by | Details |
|---|---|---|
| Browser session | Dashboard UI | `POST /auth/login` exchanges a dashboard token for a random 256-bit session id in an HttpOnly cookie with `SameSite=Strict` and `Path=/`. With HTTPS the cookie is `Secure` and `__Host-` prefixed. Only a SHA-256 hash of the id is stored. Sessions end on logout, after 60 min idle, after 12 h (7 days with "keep me signed in"), or when the token that created them is rotated. |
| Bearer token | Scripts | `Authorization: Bearer <token>`. Switch off with `BEARER_AUTH_ENABLED=false`. |

**Authorization**
- **Owner** can read and write. **Viewer** can only read.
- Every mutation checks the role on the server.
- Only manual payments are editable. Provider-verified records are read-only.

**CSRF.** A write made with the session cookie must pass all of these checks:
- carry `X-CSRF-Token`, an HMAC derived from the HttpOnly session id
- come from an allowed `Origin`: this host, or `CORS_ORIGINS`
- not be marked `Sec-Fetch-Site: cross-site`

Bearer requests are not CSRF-able, because browsers never send that header automatically.

**Brute force**
- Failed logins are limited per IP: `LOGIN_MAX_FAILURES` per `LOGIN_WINDOW_MINUTES`.
- Across all IPs the limit is 10× that.
- Failed bearer attempts are limited separately, at 4× the per-IP login limit.
- Limits are stored in the database, so they survive restarts.

**Host and size**
- Requests with a `Host` outside `ALLOWED_HOSTS` are refused, which prevents DNS rebinding.
- Bodies over `MAX_REQUEST_BYTES` get a 413, whether the size was declared or not.

**Input validation**
- Pydantic validates everything and rejects unknown fields.
- Money must be a decimal string, never a float.
- Page sizes, offsets and search text are capped.
- Search text with control characters is rejected; `%` and `_` match literally.

**SQL.** Only the SQLAlchemy ORM and bound parameters are used; queries are never built from strings.

**Errors**
- Error responses use a fixed envelope with a request ID.
- Exception text and upstream provider or SMTP responses are never returned.

**Browser hardening**
- The UI is served with a strict Content-Security-Policy (`script-src 'self'`), `X-Frame-Options: DENY`, `nosniff` and `no-referrer`.
- API responses use `default-src 'none'` and are never cached.

**Audit log** (`audit_logs`)
- sign-in, failed sign-in, sign-out, and sign out all sessions
- payment created, edited, deleted or applied to a balance
- threshold and settings changes
- manual balance entries and manual syncs
- threshold evaluations, alert retries and test emails
- backups

## Secrets

- **Where they live:** only in `backend/.env` (or real environment variables). They are read by typed settings as `SecretStr` and never stored in SQLite.
  - A test dumps the whole database after use and checks that no credential appears.
- **Never exposed:** secrets are not returned by any endpoint, including health, readiness and `/notifications/status`.
- **Logs:** a redacting formatter scrubs every log line, covering registered secret values, key/token patterns and sensitive field names.
- **Version control:** `.env` and `.env.*` are git-ignored. `.env.example` contains placeholders only.
- **Tests:** they need no real secrets. Providers, AWS (Stubber) and SMTP (a local sink) are mocked or injected.
- **File permissions:** run `scripts/harden_permissions.ps1` (Windows) or `scripts/harden_permissions.sh` (Linux/macOS). `GET /api/v1/admin/operations` warns if `.env`, the database or backups are readable by other accounts.

### Rotating credentials

| Credential | How to rotate |
|---|---|
| `DASHBOARD_OWNER_TOKEN` / `DASHBOARD_VIEWER_TOKEN` | Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`, update `.env` and restart. All sessions created with the old token stop working immediately. |
| `OPENAI_ADMIN_KEY` | Create a new Admin key in OpenAI → update `.env` → restart → delete the old key. |
| `DEEPGRAM_API_KEY` | Create a new key (role needs `billing:read` and `usage:read`) → update → restart → delete the old key. |
| `ELEVENLABS_API_KEY` | Create a new key → update → restart → delete the old key. |
| AWS access keys | Prefer an instance role. Otherwise create a second access key for the monitoring IAM user, update → restart → deactivate, then delete, the old key. |
| `SMTP_PASSWORD` | Create a new app password → update → restart → revoke the old one. |

After any suspected leak, call `POST /api/v1/admin/sessions/revoke-all` and rotate the dashboard tokens.

### Least privilege

- **AWS:** grant only [`aws-monitoring-iam-policy.json`](aws-monitoring-iam-policy.json). It contains read-only Cost Explorer, Billing GetCredits, EC2 Describe and CloudWatch read actions; the dashboard never creates, stops or deletes anything.
- **OpenAI:** an Admin key is required for the usage and cost APIs. Keep it only on this server.
- **Deepgram:** a key with billing and usage read.
- **SMTP:** a dedicated sending account.
- **Viewer token:** give it to anyone who only needs to look.

## Reliability

**Isolation**
- A failure in one provider never stops the others: errors are caught per operation, and each provider has its own timeouts and backoff.
- HTTP calls have timeouts of at most 60 s and at most 5 retries. AWS calls use standard mode with 3 attempts. SMTP calls have a timeout.
- Each operation also has an overall time budget.

**No duplicate jobs**
- Each provider poll and the alert cycle hold a database lease lock (`job_locks`) as well as an in-process lock. This prevents duplicates across processes and restarts.
- A crashed holder's lease expires and is taken over.

**Transactions**
- Payments, reconciliations, threshold and alert state, and outbox events are each written together with their audit row, or not at all.
- Optimistic version locks and unique dedupe keys guard against concurrent writers.

**Shutdown**
- On SIGTERM, live-update streams close, background tasks are cancelled, in-flight polls are recorded as interrupted with their locks released, and then clients and the database are closed.
- Run uvicorn with `--timeout-graceful-shutdown 15`.

**Tracing.** Every request has `X-Request-ID`. Background jobs get IDs like `poll-<provider>-<id>`. All logs are structured JSON with secrets redacted.

**Storage**
- If the disk fills, the API returns `507 storage_full` and logs a critical error.
- `/ready` reports `disk: failed` below `DISK_MIN_FREE_MB`.

**Backups**
- A daily SQLite online backup is integrity-checked before it is kept, and the newest `BACKUP_KEEP` are retained.
- Run one on demand with `POST /api/v1/admin/backup`.
- Failures are recorded, logged as critical, and shown to the owner as a banner and in `/admin/operations`.
- `/ready` reports `backups: warning` when overdue.
- To restore: stop the service, copy a `backups/monitor-*.db` file over `data/monitor.db`, and start it again.

**Readiness:** `GET /api/v1/ready` checks the database, migrations, disk, scheduler and backups. It returns 503 only on hard failures.

## Deployment checklist

The step-by-step procedure, with exact commands, is in [DEPLOYMENT.md](DEPLOYMENT.md).


1. `APP_ENV=production`. This disables API docs and forces Secure cookies.
2. Serve over **HTTPS** behind a reverse proxy, and set `ALLOWED_HOSTS` to your domain. Run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy ip>` so rate limits see real client IPs.
3. Use **one** uvicorn worker, with `--timeout-graceful-shutdown 15`.
4. Run the file-permission hardening script and confirm `/api/v1/admin/operations` shows no findings.
5. Consider `BEARER_AUTH_ENABLED=false` if no scripts need API access.
6. Do not expose the service to the internet without HTTPS. Bind to `127.0.0.1` or a private network.

## Verification

- **Backend tests** (`backend/tests/test_security.py` among them) cover:
  - authentication bypass, session expiry, rotation and revocation
  - CSRF, Origin and Fetch-Metadata checks
  - login and bearer rate limits, host and size limits
  - SQL injection, LIKE-wildcard and NUL attacks
  - malformed and hostile input
  - secrets in the database, responses and logs
  - storage-full handling, backups and their failure reporting
  - lease locks and cancellation
- **Browser tests** (`frontend/e2e`) check the real cookie flags, that scripts can't read the cookie, that nothing is in storage, that CSRF is enforced, the viewer restrictions, the CSP, and the accessibility scans.
- **Dependencies:** `pip-audit` (backend venv) and `npm audit` (frontend) report no known vulnerabilities as of 2026-10-14. Re-run them before each release.

## Remaining risks (accepted or to be handled at deployment)

- **No TLS in the app itself.** Production needs HTTPS termination in front. Over plain HTTP, cookies are not `Secure` (allowed only in development).
- **Long-lived static dashboard tokens** are the root credential. They are 32+ random characters and rotatable, but there is no MFA or per-user identity. That suits a single owner, not a team.
- **A leaked session is valid until it expires.** HttpOnly cookies and a strict CSP make theft unlikely, and the session can be ended immediately with "revoke all".
- **Rate limits are per IP.** Behind a proxy, configure forwarded headers, or every client shares one IP.
- **SQLite and a single process.** Lease locks protect jobs, but the app is designed for one instance. Backups are local; copy `backups/` off the machine for disaster recovery.
- **File permissions on Windows** are checked and warned about, not changed automatically. Run the hardening script.
- **Provider API keys are only as safe as the host.** Anyone with OS access to the service account can read `.env`.
- **Not yet tested against live provider accounts,** because no real keys were available during development.
