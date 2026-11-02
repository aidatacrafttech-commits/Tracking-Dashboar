# Deploying the API Cost Monitor on AWS EC2

This guide is for the person running the server. Every command here was run in a full rehearsal (§12); the remaining step is to run them on the real EC2 host.

The monitor installs **next to** the main application and never touches it:

| | Main application (adhyaapak-ai) | API Cost Monitor |
|---|---|---|
| Process | gunicorn/uvicorn on 8001, TutorDatabase on 8002 | `api-cost-monitor.service`: uvicorn on **127.0.0.1:8100**, 1 worker |
| User | existing | `apimon` (system user, no shell) |
| Code | existing | `/opt/api-cost-monitor/releases/<version>`, with `current` pointing at the live one |
| Config | its own `.env` | `/etc/api-cost-monitor/monitor.env` (root:root 0600) |
| Data | MongoDB Atlas | `/var/lib/api-cost-monitor/monitor.db` and `backups/` (apimon, 0700) |
| Logs | existing | journal namespace `api-cost-monitor` (capped at 200 MB / 30 days) |
| nginx | existing sites | nothing in private mode; one **new** site file in public mode |
| AWS identity | its own keys | read-only monitoring policy only (§7) |

**Request path:** browser → (SSH tunnel *or* nginx with HTTPS) → 127.0.0.1:8100.

**Inside the service**
- The FastAPI app serves both the API and the built UI.
- One background scheduler handles polling, alert evaluation, the email outbox and daily backups.
- The data lives in SQLite.

**Outbound connections:** provider APIs, SMTP and AWS APIs.

## 1. Local development and production

| Setting | Local development (Windows) | Production (EC2) |
|---|---|---|
| Start | `uvicorn … --reload` or the Vite dev server | systemd unit, `--workers 1`, restarts on failure, starts at boot |
| `APP_ENV` | `development` | `production` (set in the unit): API docs off, Secure cookies forced |
| Config file | `backend/.env` | `/etc/api-cost-monitor/monitor.env`, read by systemd as root; the service user cannot read it |
| Database | `backend/data/monitor.db` | `/var/lib/api-cost-monitor/monitor.db` |
| Backups | `backend/backups/` | `/var/lib/api-cost-monitor/backups/` (daily, 14 kept), plus pre-deploy and pre-restore copies |
| UI | Vite on :5173, proxying `/api` | the built `frontend/dist`, served by the backend |
| Access | http://localhost | SSH tunnel to http://localhost:8100, or https://*your-host* through nginx |
| Cookies | `acm_session`, not Secure on plain http | `__Host-acm_session`, Secure, HttpOnly, SameSite=Strict |
| Dependencies | `requirements.txt` (+ dev tools) | `requirements.lock`: the exact Linux set the tests passed with |
| Logs | console | journald namespace, JSON lines, secrets redacted |
| Email | off (`EMAIL_DELIVERY_ENABLED=false`) | off until a controlled test passes |
| Python | 3.12 (Windows venv) | 3.12 (venv per release) |

## 2. Decisions to make before deploying

1. **Access mode**
   - **Private, recommended to start.** You reach the dashboard through an SSH tunnel. There is no DNS change, no nginx change and no new open port.
   - **Public.** Choose a hostname such as `monitor.adhyaapak.ai`, add DNS, open ports 80/443, get a certificate and add an nginx site. Do this only if you need access without SSH, for example from a phone.
2. **AWS identity** (§7): attach an instance role, or create a dedicated read-only IAM user.
3. **SMTP:** choose a provider and account. EC2 blocks outbound port 25, so use 587 or 465.
4. **Python 3.12 on the host:**
   - Ubuntu 24.04 has it.
   - On Ubuntu 22.04 or Amazon Linux 2023 it must be installed. This is a package change on the shared host, so it needs your approval.

## 3. Inspect the host (read-only)

```bash
# from your PC (adjust key, user and host)
scp release/api-cost-monitor-<version>.tar.gz <ssh-user>@<ec2-host>:/tmp/
ssh <ssh-user>@<ec2-host>
mkdir -p /tmp/acm && tar -xzf /tmp/api-cost-monitor-<version>.tar.gz -C /tmp/acm --strip-components=1
sudo bash /tmp/acm/deploy/inspect_host.sh | tee ~/acm-host-inventory.txt   # changes nothing
python3 /tmp/acm/deploy/check_outbound.py --smtp-host <smtp-host> --smtp-port 587
```

`inspect_host.sh` reports:
- OS, CPU, memory and disk
- Python versions
- listening ports, and whether 8100 is free
- running services and the nginx `server_name`/`listen` lines
- certificate names and expiry dates
- the instance role name
- free disk space and journal size

It never reads `.env` files, process environments or keys.

`check_outbound.py` performs DNS, TCP and TLS handshakes only. It sends no requests and no credentials.

**Stop and review the inventory before going on.** In particular, check that port 8100 is free, Python 3.12 is present, and there's enough disk and memory. Measured in the rehearsal: about 110 MB of RAM when idle (the unit caps it at 768 MB), about 135 MB of disk per release (up to 5 are kept), plus the database and backups, which are small (240 KB to start).

## 4. Build a release (on your PC)

```powershell
cd "C:\Users\ay479\Desktop\API Tracking Dashboard"
cd frontend; npm run build; cd ..
backend\venv\Scripts\python -m pytest -q backend\tests     # must pass
backend\venv\Scripts\python deploy\build_release.py
# -> release\api-cost-monitor-<version>.tar.gz and .sha256
```

**What goes in the release:** the backend, migrations, `requirements.lock`, the built UI, `deploy/` and `docs/`.

**What doesn't:** the build refuses any `.env`, database, key file, test, virtualenv or text that looks like a credential.

Copy the archive and its checksum to the server:

```bash
scp release/api-cost-monitor-<version>.tar.gz* <ssh-user>@<ec2-host>:/tmp/
ssh <ssh-user>@<ec2-host> 'cd /tmp && sha256sum -c api-cost-monitor-<version>.tar.gz.sha256'
```

## 5. First installation

On the server:

```bash
# 0. Python 3.12 + venv, if the inventory shows it missing (needs your approval):
#    Ubuntu 24.04:  sudo apt-get install -y python3.12-venv
#    Amazon Linux:  sudo dnf install -y python3.12

# 1. Host setup: creates user apimon, directories, the config file (from the example,
#    never overwriting), the systemd units and the journal namespace. Starts nothing.
sudo /tmp/acm/deploy/manage.sh setup-host

# 2. Configure (secrets go here only; the editor works on a temporary copy)
sudo -e /etc/api-cost-monitor/monitor.env
#    Required: DASHBOARD_OWNER_TOKEN (and DASHBOARD_VIEWER_TOKEN). Generate each with
#    python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#    Add provider keys you have; leave the rest blank. Keep EMAIL_DELIVERY_ENABLED=false for now.

# 3. Install and start. Migrates the database, starts the service, waits for readiness and
#    runs the smoke test; stops with a clear error if anything fails.
sudo /tmp/acm/deploy/manage.sh deploy /tmp/api-cost-monitor-<version>.tar.gz

# 4. Status
sudo /opt/api-cost-monitor/current/deploy/manage.sh status
```

Optional systemd sandbox report: `systemd-analyze security api-cost-monitor`.

## 6. Reaching the dashboard

### Mode A: private (SSH tunnel)

Run this on your PC:

```bash
ssh -N -L 8100:127.0.0.1:8100 <ssh-user>@<ec2-host>
```

Then open **http://localhost:8100** and sign in with the owner token.

- Browsers treat `localhost` as secure, so the Secure session cookie works over the tunnel.
- Keep `ALLOWED_HOSTS=127.0.0.1,localhost` and `DASHBOARD_URL=http://localhost:8100`.

### Mode B: public HTTPS (only after approval)

1. DNS: add an `A` record for `monitor.<your-domain>` pointing at the instance's Elastic IP.
2. Security group: allow inbound 80 and 443 (from your IPs only, if you can). Keep 8100 **closed**.
3. Install the site. This adds a new file only; existing sites are untouched.

   ```bash
   sed 's/monitor.example.com/monitor.<your-domain>/g' /opt/api-cost-monitor/current/deploy/nginx/api-cost-monitor.conf > /tmp/acm.conf
   # Ubuntu:
   sudo install -m 0644 /tmp/acm.conf /etc/nginx/sites-available/api-cost-monitor.conf
   # Amazon Linux: sudo install -m 0644 /tmp/acm.conf /etc/nginx/conf.d/api-cost-monitor.conf
   ```

4. Get the certificate. First enable only the port-80 block: comment out the 443 server block, or issue the certificate before linking the site.

   ```bash
   sudo mkdir -p /var/www/letsencrypt
   sudo ln -s ../sites-available/api-cost-monitor.conf /etc/nginx/sites-enabled/   # Ubuntu
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot certonly --webroot -w /var/www/letsencrypt -d monitor.<your-domain>
   ```

   Then restore the 443 block and reload:

   ```bash
   sudo nginx -t && sudo systemctl reload nginx     # reload, never restart: no downtime for the main app
   ```

5. In `monitor.env`, set:
   - `ALLOWED_HOSTS=monitor.<your-domain>,127.0.0.1,localhost`
   - `DASHBOARD_URL=https://monitor.<your-domain>`

   Then run `sudo systemctl restart api-cost-monitor`.
6. Verify from your PC: `python deploy/smoke_test.py --base-url https://monitor.<your-domain> --login`

**What the site provides**
- TLS 1.2/1.3, HSTS, and an HTTP→HTTPS redirect
- sign-in throttling (10/min per IP, on top of the app's 5 failures per 15 minutes)
- a 64 KB body limit
- unbuffered Server-Sent Events for live updates
- `X-Forwarded-For` set to the real client address. Uvicorn trusts it only from 127.0.0.1, so rate limits see real IPs.

## 7. AWS permissions (least privilege)

Grant **only** [`aws-monitoring-iam-policy.json`](aws-monitoring-iam-policy.json): Cost Explorer read, Billing `GetCredits`, EC2 `Describe*`, and CloudWatch read for S3 metrics. The monitor never creates, changes, stops or deletes anything.

- **Preferred: instance role.**
  - Attach a role with this policy to the instance, or add the policy to the existing role.
  - Leave every `AWS_*` key blank in `monitor.env`, and set `AWS_MONITORING_ENABLED=true`.
  - No long-lived key is stored on disk.
  - Trade-off: every process on the instance, including the main app, can then use these read-only permissions.
- **Alternative: a dedicated IAM user**, `api-cost-monitor-readonly`, with only this policy. Put its access key in `monitor.env`.
- **Never** reuse the main application's AWS keys or any administrator credentials.
- Cost Explorer must be enabled once in the Billing console. It's billed at **USD 0.01 per request**: with the 6-hour schedule that's about 12 requests a day, roughly USD 3.60 a month.

## 8. Outbound access

**Default EC2 security groups allow all outbound traffic.** If yours restricts egress, allow TCP 443 to:
- `api.openai.com`, `api.deepgram.com`, `api.elevenlabs.io`
- `status.openai.com`, `status.deepgram.com`, `status.elevenlabs.io`, `status.sarvam.ai`
- `ce.us-east-1.amazonaws.com`, `billing.us-east-1.api.aws`, `sts.us-east-1.amazonaws.com`
- `ec2.<region>.amazonaws.com`, `monitoring.<region>.amazonaws.com`
- `pypi.org` and `files.pythonhosted.org`, at deploy time only

Also allow your SMTP host on **587** or 465. Port 25 is blocked by AWS.

**Check:** run `deploy/check_outbound.py`.

## 9. Scheduling, intervals and rate limits

There is exactly one scheduler, because the unit runs one uvicorn worker. **Never raise `--workers`.** If a second process ever starts by mistake, database leases still stop duplicate polls, alert cycles and backups.

| Job | Interval | Why |
|---|---|---|
| Balances and usage (OpenAI, Deepgram, ElevenLabs) | 30 min (`POLL_INTERVAL_MINUTES`) | Keeps balances fresh (stale after 2 h); a few requests per poll, far below every provider's limits. A 429 response is backed off, honouring `Retry-After`. |
| Status pages and probes | 10 min | Public status JSON |
| AWS Cost Explorer (costs, forecast, per-service) | 360 min (`AWS_COST_EXPLORER_INTERVAL_MINUTES`) | AWS refreshes billing data a few times a day; each request costs money |
| AWS credits, EC2/EBS state, S3 metrics | 30 min | Free describe and read calls |
| Threshold evaluation and email outbox | every minute | No external calls unless an email is due |
| Database backup | 24 h, 14 kept | Plus pre-deploy and pre-restore copies |

## 10. Logs, health and disk

**Logs** go to journald namespace `api-cost-monitor`, capped at 200 MB / 30 days, separate from the system journal:

```bash
sudo journalctl --namespace=api-cost-monitor -u api-cost-monitor -f          # follow
sudo journalctl --namespace=api-cost-monitor -u api-cost-monitor -p err --since today
```

Lines are JSON, with a request ID and no secrets. nginx logs (public mode) go to `/var/log/nginx/api-cost-monitor.*.log`, which the existing nginx logrotate already rotates.

**Health URLs** (the private ones work on the server or through the tunnel):

| URL | Meaning |
|---|---|
| `http://127.0.0.1:8100/api/v1/health` | Process is alive (no database access) |
| `http://127.0.0.1:8100/api/v1/ready` | Database, migrations, disk, backups and scheduler. 503 only on hard failure; `warning` means degraded |
| `https://monitor.<domain>/api/v1/ready` | The same, through nginx (public mode) |
| `/api/v1/admin/operations` (owner sign-in) | Backups, disk, file permissions, sessions, failed sign-ins and failed emails. The owner also sees a banner in the UI |

**Health-check timer:** `api-cost-monitor-healthcheck.timer` runs every 5 minutes. It checks readiness and that at least 1 GB is free under `/var/lib/api-cost-monitor`. A failure is logged as an error and the unit stays failed, so it shows in `systemctl --failed`. The app's own disk check marks readiness as failed below `DISK_MIN_FREE_MB`, and a full disk returns `507 storage_full`.

For alerts that reach you while you're away, add a CloudWatch alarm on the instance status check, and optionally the CloudWatch agent's `disk_used_percent`. This is optional and not part of this kit.

## 11. Backup, restore, update, rollback

**Commands**

| Task | Command |
|---|---|
| Backup now (online, verified) | `sudo /opt/api-cost-monitor/current/deploy/manage.sh backup` |
| Copy backups off the server | `scp <ssh-user>@<ec2-host>:/var/lib/api-cost-monitor/backups/monitor-*.db .` (the directory is apimon-only: copy them to your login user with `sudo` first, or use `sudo tar` and `scp`) |
| Restore | `sudo /opt/api-cost-monitor/current/deploy/manage.sh restore /var/lib/api-cost-monitor/backups/<file>.db` |
| Update | Build a release (§4), copy it, extract it, then run **the new release's** script: `sudo /tmp/acm-new/deploy/manage.sh deploy /tmp/api-cost-monitor-<new>.tar.gz` |
| Roll back | `sudo /opt/api-cost-monitor/current/deploy/manage.sh rollback` (or `rollback <release-dir-name>`) |

**What restore does**
1. Integrity-checks the file.
2. Stops the service.
3. Keeps the current database as `pre-restore-*.db`.
4. Installs the backup and clears runtime leases.
5. Migrates the restored database to the installed schema, so an older backup works.
6. Starts the service and waits for readiness.

**What a deploy does**
1. Installs into a new release directory, with its own venv built from `requirements.lock`, and checks that it imports.
2. Stops the service.
3. Takes a verified `pre-deploy-*.db` backup.
4. Switches `current`, migrates and starts.
5. Waits for readiness, then runs the smoke test.
6. Keeps the last 5 releases.

Downtime is about 4 seconds.

**Automatic rollback:** if any step after the switch fails, the deploy switches `current` back. If the schema changed, it restores the pre-deploy backup. It then starts the old release and exits with an error. The failed release is kept in `/opt/api-cost-monitor/failed-*` for inspection.

**Manual rollback:** switches to the previous release. It restores the pre-deploy database only if the newer release changed the schema; the current data is kept as `pre-rollback-*.db` first.

## 12. Rehearsal (what has been verified, and where)

The whole kit was rehearsed on 2026-10-15 in WSL Ubuntu 24.04 (Python 3.12.3, systemd 255, nginx 1.24.0) by `deploy/rehearsal/rehearse_wsl.sh`. The same `manage.sh`, units, smoke test and nginx site were used.

**Differences from EC2**
- Paths were under `~/acm-rehearsal`.
- It used a `systemctl --user` service instead of a system service with the `apimon` user, so the root-only sandbox options and the journal namespace were not exercised.
- nginx ran unprivileged on ports 8443/8080 with a self-signed certificate.
- Email went to a local SMTP sink.
- No provider credentials were used.

**Results**
- **Tests:** the backend test suite passed on Linux with the exact `requirements.lock`: 340, then 341 after the backup fix.
- **Unit files:** `systemd-analyze verify` found no invalid directives in the shipped units, after one was fixed.
- **First install and updates:** a clean install migrated 0001→0005 and the smoke test passed. Six updates followed, each with a pre-deploy backup, about 4 s of downtime and a passing smoke test.
- **Restart and reboot:** a restart and a full Linux shutdown/start both shut down gracefully ("application stopped") and came back automatically. Thresholds, payments and balances were intact.
- **Manual rollback:** worked.
- **Automatic rollback:** a deliberately broken release (a schema change plus a startup failure) was rolled back automatically. The database returned to revision 0005, the service was ready, and the data was intact.
- **Backups and restore:** a manual backup → marker payment → restore removed the marker and kept the earlier data. The same worked with the app's own scheduled backup, after which the status was "ok" and the next backup ran immediately.
- **HTTPS through nginx 1.24:** `nginx -t` passed; HTTP redirects to HTTPS; HSTS is present; the cookie is `__Host-acm_session`, Secure; the signed-in smoke test passed; sign-in is throttled to 429 after 5 failures; an unknown Host header gets 400.
- **Dashboard checks:**
  - All 5 providers listed, each with its source, freshness and unsupported operations.
  - Independent thresholds (Deepgram in USD, Sarvam in INR).
  - The controlled test email was received by the sink.
  - No secrets in any API response or in the 12 UI script files.
- **Real browser (Chrome), both access modes:** http://localhost:8100 (the tunnel case) and https://localhost:8443 (through nginx).
  - Sign-in works; the cookie is `__Host-acm_session` (HttpOnly, Secure, Strict) and scripts can't read it.
  - Nothing is in browser storage.
  - Live updates connect.
  - All 5 providers render, and the AWS page keeps infrastructure and billing separate.
- **Cross-site writes:** a foreign Origin gets 403 `origin_not_allowed`; a missing or wrong CSRF token gets 403 `csrf_failed`; nothing was written.
- **Outbound check from Linux:** all 14 endpoints reachable. It also caught a wrong hostname in the check list (`billing.us-east-1.api.aws` is the real Billing endpoint, confirmed by capturing botocore's request offline).

**Defects found by the rehearsal and fixed**
- Start-limit settings were in the wrong unit section, so systemd ignored them.
- Rollback could not start the old release after a crash loop, because systemd's start limit was hit. Fixed with `reset-failed`.
- Migrations created the database as 0644. It is now 0600.
- Deploys ran the old release's helper tools.
- Backups left `-wal`/`-shm` files beside them.
- The app's backups carried runtime leases and a "running" backup record. After a restore, they showed "Last backup failed" and blocked backups for up to 30 minutes. Fixed in the app, with a regression test.

## 13. Final acceptance checklist (run on EC2)

| Item | How to check | Rehearsal result |
|---|---|---|
| All configured providers appear | `smoke_test.py --login`: "all providers present" table | ✅ (5/5) |
| Each provider shows its data source and sync freshness | Same table, plus each provider page | ✅ |
| Unsupported balance APIs are clearly identified | "unsupported operations" line; provider pages | ✅ (OpenAI and Sarvam balance) |
| Thresholds are independently configurable | Set two providers in the UI; both persist | ✅ (USD and INR) |
| Email works in a controlled test | `smoke_test.py --send-test-email you@yourdomain` (only that address), or Thresholds → Send test email | ✅ to the local sink; **real SMTP not yet tested** |
| Payment history persists | Add one, restart, check | ✅ restart, reboot and rollback |
| AWS infrastructure and costs shown separately | AWS page: "Infrastructure health" vs "Billing" | ✅ (endpoints separate; AWS disabled in the rehearsal) |
| Dashboard survives a restart | `sudo systemctl restart api-cost-monitor`, then `sudo reboot` (in a maintenance window) | ✅ restart and simulated reboot |
| Authentication and HTTPS | Sign-in required; 401 without it; HTTPS and HSTS in public mode | ✅ (self-signed in the rehearsal) |
| No secrets reach the frontend | The smoke test scans the bundle and responses | ✅ |
| Backup and recovery tested | `manage.sh backup`, then `restore` | ✅ |

## 14. Service names and paths

| Name | What it is |
|---|---|
| `api-cost-monitor.service` | The dashboard (enabled at boot) |
| `api-cost-monitor-healthcheck.timer` / `.service` | Readiness and disk check every 5 minutes |
| `journald@api-cost-monitor.conf` | Log size limits for the namespace |
| `/opt/api-cost-monitor/current` | Live release (symlink) |
| `/etc/api-cost-monitor/monitor.env` | Configuration and secrets (root:root 0600) |
| `/var/lib/api-cost-monitor/` | Database and `backups/` (apimon, 0700) |
| `/etc/nginx/…/api-cost-monitor.conf` | Public mode only |

## 15. Dependency lock

`backend/requirements.lock` is the exact runtime set, resolved on Linux with Python 3.12. To regenerate it after changing `requirements.txt`, run this in WSL or any Linux machine with Python 3.12, then run the tests against it:

```bash
python3.12 -m venv /tmp/lock && /tmp/lock/bin/pip install -r backend/requirements.txt
/tmp/lock/bin/pip freeze --all --exclude pip --exclude setuptools --exclude wheel > backend/requirements.lock
```

The Windows development venv may have slightly different transitive versions. The lock is what production runs.

## 16. Removing the monitor

This stops only the monitor; the main application is untouched.

```bash
sudo systemctl disable --now api-cost-monitor api-cost-monitor-healthcheck.timer
sudo rm /etc/systemd/system/api-cost-monitor*.service /etc/systemd/system/api-cost-monitor*.timer /etc/systemd/journald@api-cost-monitor.conf
sudo systemctl daemon-reload
# keep or archive /var/lib/api-cost-monitor (data) before:
sudo rm -rf /opt/api-cost-monitor /etc/api-cost-monitor
# public mode: remove the nginx site file and reload nginx
```
