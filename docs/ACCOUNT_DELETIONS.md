# Account Deletions

Admin screen for AdhyaapakAI student accounts in the deletion grace period
(nav: **Account Deletions**, route `/account-deletions`).

## How the secret stays server-side

```
browser ──(session cookie + CSRF)──▶ dashboard backend /api/v1/account-deletions/*
                                         │  adds  x-admin-secret: $ADHYAAPAK_ADMIN_SECRET
                                         ▼
                                    AdhyaapakAI  /api/admin/accounts/*
```

- The secret lives only in `backend/.env` (`ADHYAAPAK_ADMIN_SECRET`, a `SecretStr`, redacted
  from logs). It is never sent to the browser and is not in the frontend build.
- The browser never calls AdhyaapakAI, so **no origin needs to be added to AdhyaapakAI's
  `ALLOWED_ORIGINS`** (the call is server-to-server; CORS does not apply).
- Logs record method, path and status only; response bodies (emails, names) are never logged.

## Configuration (`backend/.env`, restart the dashboard after changing)

| Variable | Value |
| --- | --- |
| `ADHYAAPAK_API_BASE_URL` | `http://127.0.0.1:8001` (local) or `https://api.adhyaapak.ai` (production) |
| `ADHYAAPAK_ADMIN_SECRET` | Must equal the AdhyaapakAI backend's `ADMIN_SECRET` |

Either empty: the page shows "Account deletions are not configured". Wrong secret: the
page shows "Not authorised."

## Dashboard endpoints

| Method | Path | Who | Upstream |
| --- | --- | --- | --- |
| GET | `/api/v1/account-deletions/status` | owner, viewer | none (`{configured, target}`) |
| GET | `/api/v1/account-deletions` | owner, viewer | `GET /api/admin/accounts/deletions` |
| POST | `/api/v1/account-deletions/reactivate` | owner + CSRF | `POST /api/admin/accounts/reactivate` |
| POST | `/api/v1/account-deletions/purge-now` | owner + CSRF | `POST /api/admin/accounts/purge-now` (`force` always `false`) |

Request body for both actions: `{email, note}`. The note is required here (3–500 chars) even
though AdhyaapakAI accepts it empty, because it is the only record of why. Sending `force`
is rejected with 422.

Error mapping: upstream 400/404/409/410 pass through with their `detail` as the message;
upstream 401/403 become 502 `adhyaapak_forbidden`; timeouts 504, unreachable 502, 5xx 502.
Successful actions are written to this dashboard's audit log (`account.reactivate`,
`account.purge_now`) with a masked email, the user id and the note.

## Screen behaviour

- Header shows the count and grace period; times are in IST; "Days left" ≤ 1 is red.
- Client-side search by email, Refresh button, auto-refresh every 60 s.
- **Restore**: needs the "I verified this request came from the registered email address"
  checkbox and a note. `already: true` counts as success. 409/410 show the backend detail in
  a toast and reload the list.
- **Purge now**: the admin types the email and a note. A 409 is shown in the dialog; there
  is no retry and no force option.
- Buttons are disabled while a request is in flight. Viewers see the list without actions.
