# App Users

Read-only analytics over the AdhyaapakAI app's users: new signups per day (any range up to 90
days), the signups of any day or date range with their contact details, activity, course purchases
and deletions (nav: **App Users**, route `/app-users`).

## What a "signup" is

A signup is a completed registration: a `users` document, created when the student finishes
`POST /api/users/register`. Email OTP alone creates no account. The app records **no install
or first-open event**, so real install counts exist only in Play Console and App Store
Connect; the page says so.

Days are calendar days in the dashboard's display timezone (`Asia/Kolkata` here, the same
timezone the app uses). A student who signs up at 23:59 IST and one who signs up at 00:01 IST
land on different days.

## How the database stays safe

```
browser ──(session cookie)──▶ dashboard backend /api/v1/app-users/*
                                  │  find / count_documents only, maxTimeMS 10 s
                                  ▼
                             MongoDB (ADHYAAPAK_MONGO_URL, database ADHYAAPAK_MONGO_DB)
```

- The URL lives only in `backend/.env`. It is a `SecretStr`, and both the URL and its
  password are registered with the log redactor. It never reaches the browser, `/status`
  returns only the database name, and errors carry no driver detail.
- `MongoSource` (`backend/app/services/app_users.py`) only runs fixed `find` and
  `count_documents` queries. They read three allow-listed collections (`users`,
  `account_deletions` and `entitlement_purchases`) with tight projections. There is no write
  call in the module. Reads prefer a secondary, the pool holds at most 2 connections, and
  the client connects lazily (startup never waits on Atlas).
- Results are cached in memory for 60 seconds per range and day, so a busy dashboard does
  not load production.
- **Contact details:** the per-day signup list shows the **full name, email and phone to the
  owner only**. Viewers (the read-only token) get them masked on the server: first name,
  `a***@gmail.com` and `******3210`. Responses are `Cache-Control: no-store`, and logs carry
  the error type only, never a row.
- The App Store review account (`applereviewer@adhyaapak.ai`) is excluded everywhere.

**Use a read-only database user.** In Atlas, go to Database Access → Add New Database User,
choose built-in role **read** on database `doubt_solver_app`, and put that user's URL in
`ADHYAAPAK_MONGO_URL`. Add the dashboard server's outbound IP to the Atlas **IP Access
List**; Atlas needs outbound TCP 27017 plus DNS SRV lookups.

## Online now (live)

**Online now** counts everyone who is using the app right now, each person once, from every
live signal the app already writes. It needs no app release:

| Signal | Window | Why that window |
|---|---|---|
| Any app request (`users.last_active_at`, opening the app included) | 12 min | The app writes it at most every 10 minutes per user, so someone using the app is never missed |
| Watching a video (`video_progress`, `recentWatching`) | 25 s | The player saves progress every 5-10 s while playing |
| In Live Tutor (`ai_tutor_usage_sessions`, active) | 30 s | Heartbeat every 10-15 s |
| Asking doubts (`doubt_sessions.updated_at`) | 5 min | Updated on every message |
| Finished a test (`test_attempts.completed_at`) | 5 min | |

Opening the app, starting a video or Live Tutor shows within about 3-15 seconds. Stopping a
video drops out within about 25 seconds, Live Tutor within 30 seconds, doubts within 5 minutes. Someone who only
browses stays counted for up to about 12 minutes after their last request. That's the limit
of what the app records, since it never reports "closed". Only the user-id field is read
(doubt sessions are never fetched with their messages), and results are cached for
3 seconds (one database read per 3 seconds, however many dashboards are open). The card
refreshes every 3 seconds.

**Exact open/close (optional, needs an app release):** the mobile app can also send
heartbeats while it is open:

- The app (`adhyaapak-ai/frontend/services/presence.ts`, started from `App.tsx`) sends
  `POST /api/v1/integrations/presence/heartbeat` with `{"state": "active"}` every 30 s in
  the foreground, and `{"state": "background"}` once on close.
- Those users are added to the same count. A closed app drops out at once, and a killed one
  after 75 s.
- Each heartbeat carries the student's normal login token (HS256 JWT). The dashboard verifies
  it with `ADHYAAPAK_JWT_SECRET` (the AdhyaapakAI backend's `JWT_SECRET`) without calling
  that backend, and counts distinct users. Dashboard tokens are refused, and the token is
  never logged. Successful heartbeats are logged at DEBUG only.
- Presence is kept in memory (the dashboard runs one worker). A restart starts from 0, and
  open apps are counted again within 30 s.

**Before it can count anyone:**

1. The dashboard must be reachable from phones: public HTTPS mode in `docs/DEPLOYMENT.md`.
   The nginx site has a dedicated heartbeat location that stays open even when the
   allow/deny list is used, with its own rate limit and no access log.
2. Set `ADHYAAPAK_JWT_SECRET` in the dashboard's `backend/.env` and restart the dashboard.
3. Set `PRESENCE_URL` (or `EXPO_PUBLIC_PRESENCE_URL`) in the app's `frontend/.env` to
   `https://<dashboard host>/api/v1/integrations/presence/heartbeat`, then build and release
   the app. Only users on that version are counted.

Until then, Online now uses the signals in the table above.

## Configuration (`backend/.env`, restart the dashboard after changing)

| Variable | Value |
| --- | --- |
| `ADHYAAPAK_MONGO_URL` | `mongodb+srv://<read-only user>:<password>@cluster0.xxxxx.mongodb.net/?appName=Cluster0` |
| `ADHYAAPAK_MONGO_DB` | `doubt_solver_app` |
| `ADHYAAPAK_JWT_SECRET` | The AdhyaapakAI backend's `JWT_SECRET`, used to verify the app's heartbeats (optional) |

Both empty: the page shows "App users are not configured". Only one set: the dashboard
refuses to start, with a clear message. A rejected login: the page shows "Not authorised."

## Dashboard endpoints (owner and viewer)

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/v1/app-users/live` | `{online_now, app_open, using_app, watching_video, in_live_tutor, asking_doubts, taking_test, online_window_minutes, checked_at}`. `app_open` is `null` while `ADHYAAPAK_JWT_SECRET` is unset. |
| POST | `/api/v1/integrations/presence/heartbeat` | **The mobile app only**, with its login token as the bearer: `{"state": "active" \| "background"}` → 204. 401 for a missing, forged or expired token, or while presence is off. |
| GET | `/api/v1/app-users/status` | `{configured, database}`; never contacts MongoDB |
| GET | `/api/v1/app-users/summary?days=30` or `?start=YYYY-MM-DD&end=YYYY-MM-DD` | Today block (always today), totals, `daily[]`, breakdowns for the selected days. `days` is 7–90; a custom range is at most 90 days and can't end in the future. |
| GET | `/api/v1/app-users/deletions?start=YYYY-MM-DD&end=YYYY-MM-DD` (or `?date=`) | Deletion requests made in those days (dated by the request, or by the start of an immediate purge), newest first: status, due / completed / restored time, signup date, and name, email and phone **while the profile still exists**. Same masking and range rules as signups. |
| GET | `/api/v1/app-users/signups?start=YYYY-MM-DD&end=YYYY-MM-DD` (or `?date=` for one day) | Signups in those days (default today), newest first, at most 2,000 (`more: true` if there are more). Same range rules as the summary. Full contact details for the owner; `masked: true` for viewers. |

**Errors**

| Condition | Response |
| --- | --- |
| Not configured | 503 `app_users_not_configured` |
| Bad range or date | 422 `range_incomplete`, `range_reversed`, `range_in_future`, `range_too_long` |
| Timeout | 504 `app_users_timeout` |
| Login refused | 502 `app_users_forbidden` |
| Query rejected | 502 `app_users_query_failed` |
| Unreachable | 502 `app_users_unreachable` |

Money is in integer paise.

## Metrics and where they come from

| Metric | Source | Notes |
| --- | --- | --- |
| Signups per day | `users._id` (ObjectId time) | `created_at` has no index; the `_id` carries the same instant and is indexed |
| Deleted since | `account_deletions` (`status: completed`) | The journal's `_id` is the user's id, so a purged account still counts on its signup day |
| Active today / 7 / 30 days | `users.last_active_at` | Only written since 28 Oct 2026, and overwritten on each visit, so there is no daily history |
| Course purchases, revenue | `entitlement_purchases` | Refunds excluded. Vouchers are counted separately and add no revenue. Revenue is INR only; non-INR App Store purchases are counted but left out of the total. |
| Have a course | `users.entitlements` not empty | Covers this period's signups that still exist |
| By class / stream / goal | `users.class`, `stream`, `goals` | Covers accounts that still exist; a student can pick several goals |
| Deletions scheduled | `account_deletions` (`status: scheduled`) | Accounts in the 7-day grace period |
| Total users | `users` count | All accounts, reviewer excluded |

## Screen behaviour

- **Today:**
  - new signups, with yesterday's count and the difference
  - active today, with 7- and 30-day counts
  - purchases today, with ₹ revenue and vouchers
  - total users
  - deletions scheduled
- **List of people, New users or Deletions:** two buttons choose what the list shows, then
  the same From date / To date pickers, search and table.
  - **Deletions** reads the app's deletion journal (`account_deletions`): requested time,
    name, phone, email, status (Scheduled, Deleting, Deleted, Restored), due / deleted /
    restored time, and signup date. A permanently deleted account shows "Details removed":
    the app deletes the profile, and this page does not try to rebuild it from other data.
    Restore and purge stay on the Account Deletions screen (AdhyaapakAI admin API).
- **Signups list for any dates:** From date / To date pickers (default today, with a "Today"
  button) and a search box over name, email and phone.
  - Columns: time (IST), name, phone, email, class, stream, goals, and "Has course" or
    "Deletion scheduled" badges.
  - Accounts deleted since are no longer in the database, so they are not listed (the chart
    still counts them).
- **Last 7, 30 or 90 days, or custom dates** (From / To, at most 90 days; default 30 days):
  - totals, average per day, best day, "have a course" and purchases
  - a stacked daily bar chart (signups, plus signed up and since deleted) with a data table;
    clicking a bar or a date in the table sets the signups list to that day
  - class, stream and goal breakdowns
- Refresh button; auto-refresh every 60 seconds (served from the 60-second cache).
