# Course Management

**Course Management** is a module of this dashboard; it's in the sidebar. With it you can:
- create courses, organise them by class, stream and subject, and build their content (playlist → chapter → lecture → language versions + study materials);
- publish, schedule and archive courses;
- see every student's enrollment, with the validity and expiry that applied to their own purchase.

The most important rule is about validity. Each student's access starts at **their own successful purchase** and lasts the validity that applied **when they bought**. Changing a course's validity later never changes an existing student's expiry.

## Architecture

| Layer | What it uses |
|---|---|
| Admin UI | The existing React dashboard: `frontend/src/pages/CoursesPage.tsx`, `CourseEditorPage.tsx`, `pages/courses/*`. Same login, CSRF, design system and tables. |
| Admin API | `GET/POST/PUT/DELETE /api/v1/courses/...`. The owner can change things; viewers can only read. Session + CSRF, or owner bearer. |
| App API | `/api/v1/integrations/courses/...`, with **`COURSE_API_TOKEN`** bearer only. For the trusted app backend that authenticates students and verifies payments. |
| Database | The dashboard's SQLite database. Migration `0008` adds 10 tables; nothing existing is changed. |
| Files | A private S3 bucket (`COURSE_ASSETS_BUCKET`). The browser uploads directly with a presigned POST that pins the key, type and maximum size. The server then verifies the object. Students and admins get 15-minute signed URLs. |
| Payments | **Not handled here.** The app backend already talks to the payment provider. After *it* verifies a payment, it calls `POST /integrations/courses/payments/confirm`. Nothing in this dashboard duplicates the payment system. |
| Coupons | The app's existing vouchers. The app applies the voucher at checkout and reports `amount_paid` + `voucher_code`; both are saved on the enrollment. |

## Database

```
course_taxonomy_terms (class | stream | subject)
        ▲ class_id / stream_id / subject_id (RESTRICT)
courses ──< course_playlists ──< course_chapters ──< course_lectures ──< course_lecture_variants (one per language)
   │                                                        └──────< course_lecture_resources (PDF / notes)
   │  (RESTRICT: a course with enrollments can't be deleted)
   └──< course_enrollments ──< course_enrollment_events (append-only history)
course_assets (S3 key + metadata; referenced by thumbnails, videos, audio, subtitles, resources)
```

**Key rules**
- **Content tree:** each level cascades to the next (deleting a playlist deletes its chapters and lectures). Every child has a `position` for ordering.
- **Enrollments:**
  - They reference the course with `ON DELETE RESTRICT`, so a course with any enrollment can only be archived, never deleted.
  - They carry snapshots taken at purchase: `course_title`, `list_price`, `amount_paid`, `validity_value`, `validity_unit`, `access_start_at` and `access_expires_at`.
  - They hold the student only as the app's opaque `student_id`: no name or contact details (migration 0009 dropped `student_name`), so purchase history identifies nobody once the student's app account is deleted.
  - `payment_id` and `order_id` are **unique**, so repeated webhooks can't create a second enrollment.
- **Database checks:** CHECK constraints enforce positive validity, prices ≥ 0, expiry after start, one video source per language version, and the enum values.

## Validity calculation

`app/services/course_validity.py`:

```
access_expires_at = purchase time (in IST) + N days | N calendar months | N calendar years
```

- **Calendar arithmetic, not 30-day months.** 31 Jan + 1 month = 28 Feb (or 29 Feb in a leap year), and 29 Feb + 1 year = 28 Feb.
- **Done in the display timezone (IST)**, so the calendar date matches what the student sees. A purchase at 01:00 IST on 10 Nov expires at 01:00 IST on 10 May. Stored in UTC.
- **Presets:** 6, 7 or 10 months, or 1 year. Custom: 1–3650 days, 1–120 months or 1–10 years.
- **Example from the requirements (tested):** launch 1 Nov 2026, 6 months. Student A buys on 10 Nov 2026 and gets 10 Nov 2026 → 10 May 2027. Student B buys on 20 Dec 2026 and gets 20 Dec 2026 → 20 Jun 2027.
- **The launch date never starts anyone's validity.** It only decides when the course can be bought.
- **Course edits never touch enrollments.** A test changes the validity from 6 months to 1 year: the existing student keeps 6 months, and the next buyer gets 1 year.

## Purchase and access flow (app backend → dashboard)

1. **Checkout starts:** `POST /api/v1/integrations/courses/orders` with `{course_id, student_id, order_id, payment_provider}`.
   - Creates a **pending** enrollment and snapshots the validity and price the student saw.
   - Refused if the course isn't published, hasn't launched, is archived, or is free.
2. **Payment verified by the app backend:** `POST /api/v1/integrations/courses/payments/confirm` with `{course_id, student_id, payment_id, order_id?, payment_provider, amount_paid_paise, currency, paid_at?, voucher_code?}`.
   - In one transaction: activates the enrollment, sets `purchased_at = access_start_at = paid_at` (or now), computes the expiry, and writes history and audit entries.
   - **Idempotent:** the same `payment_id` returns the same enrollment with `"duplicate": true`. Tested with 12 parallel deliveries, which produce exactly one enrollment.
   - Checks: currency must match; `amount_paid_paise` ≤ price; a lower amount needs a `voucher_code`; `paid_at` can't be in the future.
3. **Free course:** `POST /api/v1/integrations/courses/enroll-free` with `{course_id, student_id}`. Starts access immediately and needs no payment. Repeating it returns the active enrollment.
4. **Refund or cancellation:** `POST /api/v1/integrations/courses/payments/reverse` with `{payment_id, status: refunded|cancelled, reason}`. Access ends; history is kept.

**Expiry**
- Every access check compares `access_expires_at` with the current time. When access has expired, the API answers `403 course_access_expired` with the expiry date.
- The status becomes `expired`, and an "expired" history entry is written the next time enrollments are read.
- Purchases and history are never deleted. A new purchase starts a new validity.

## API: admin (`/api/v1/courses`)

| Method and path | Purpose |
|---|---|
| `GET /summary` | Totals: published, scheduled, draft, archived, enrollments by status, revenue (paid, not refunded) |
| `GET /` | List. Query: `q`, `class_id`, `stream_id`, `subject_id`, `status` (draft/scheduled/published/archived), `pricing` (free/paid), `launch_from`, `launch_to`, `sort` (created/updated/title/launch_at/price), `order`, `limit` (≤ 500), `offset` |
| `POST /`, `GET /{id}`, `PUT /{id}` (needs `version`; a stale one gives `409 VERSION_CONFLICT`), `DELETE /{id}` | Course CRUD. Only a draft with no enrollments can be deleted (`can_delete`); anything else is archived. |
| `GET /{id}/publish-check`, `POST /{id}/publish` · `/unpublish` · `/archive` · `/unarchive` | Status changes. Publishing is blocked only by: no title, a paid course without a price, or no published lecture with a playable video. Missing description, class, subject, launch date or thumbnail, and lectures without video, are warnings. Unpublishing is refused while students hold or await access. |
| `POST /{id}/playlists`, `PUT /{id}/playlists/order` | Playlists |
| `PUT·DELETE /playlists/{id}`, `POST /playlists/{id}/chapters`, `PUT /playlists/{id}/chapters/order` | Chapters |
| `PUT·DELETE /chapters/{id}`, `POST /chapters/{id}/lectures`, `PUT /chapters/{id}/lectures/order` | Lectures |
| `PUT·DELETE /lectures/{id}`, `POST /lectures/{id}/variants`, `POST /lectures/{id}/resources`, `PUT /lectures/{id}/resources/order` | Language versions and materials |
| `PUT·DELETE /variants/{id}`, `PUT·DELETE /resources/{id}` | Edit or remove a language version or material |
| `GET /storage`, `POST /assets/uploads` (`{kind, filename, content_type, size_bytes, course_id?}`), `POST /assets/{id}/complete`, `GET /assets/{id}` | Uploads: ticket → browser posts to S3 → server verifies |
| `GET /taxonomy`, `POST /taxonomy`, `PUT·DELETE /taxonomy/{id}` | Classes, streams and subjects. Delete only works when unused. |
| `GET /enrollments`, `GET /enrollments/{id}`, `POST /enrollments/{id}/cancel` | Enrollments with history; cancel is an explicit admin action that needs a reason |
| `GET /{id}/preview`, `GET /{id}/preview/lectures/{lid}?language=`, `GET /{id}/preview/resources/{rid}` | Preview as a student, drafts included |

## API: app backend (`/api/v1/integrations/courses`, `COURSE_API_TOKEN`)

| Method and path | Purpose |
|---|---|
| `GET /catalog?student_id=` | Published **and launched** courses (no launch date = on sale at publish), with the student's access state. An upcoming course is only reachable by id, with `availability.reason = not_launched`. |
| `GET /{id}?student_id=` | Course details, structure (published lectures only, with languages) and access |
| `GET /{id}/access?student_id=` | `active` / `pending` / `expired` / `cancelled` / `refunded` / `none`, with the expiry and a message |
| `GET /{id}/lectures/{lid}/play?student_id=&language=` | Signed video URL for the chosen language. Free previews are open; everything else needs active access. An unknown language returns `404 language_unavailable` listing the available languages. |
| `GET /{id}/resources/{rid}/download?student_id=` | Signed download URL. Needs access and `is_downloadable`. |
| `GET /students/{student_id}/enrollments` | The student's enrollments and statuses |
| `POST /orders`, `POST /payments/confirm`, `POST /payments/reverse`, `POST /enroll-free` | Purchase flow, described above |

**Messages the app should show** (errors use the dashboard's usual `{error: {code, message, details}}` format):

| Code | Situation |
|---|---|
| `course_not_launched` | Course not launched yet |
| `enrollment_pending` | Payment pending |
| `course_access_expired` | Access expired |
| `course_archived` | Course archived |
| `language_unavailable` | Lecture language unavailable |
| `not_enrolled` | Student isn't enrolled |

## Security

- **Admin permissions:** admin writes are owner-only (session + CSRF, or owner bearer); viewers are read-only. The existing route-permission tests cover all 63 new routes automatically.
- **App token:** `COURSE_API_TOKEN` grants only the app API. It must differ from the dashboard and voucher tokens, it's compared in constant time, failed attempts are rate-limited, and it's redacted from logs. Without it, the app API answers `401 course_api_disabled`.
- **Enforced on the server:** access, expiry, downloads and publish rules are all checked in the backend, not just hidden in the UI. Prices, expiry and payment status are never taken from the browser.
- **Student ownership:** the app backend passes the student id of the student *it* authenticated, and enrollments are matched on `(course, student)`.
- **Files:**
  - the bucket stays private;
  - keys follow the AdhyaapakAI layout `course-assets/<course_id>/<asset_id>-<slug>.<ext>` (`shared/` instead of the course id for a new course's thumbnail); only a lowercase slug of the filename is used, and `videos/` or `notes/` prefixes are refused;
  - type, extension and size are checked;
  - the upload policy caps the size;
  - the server checks the stored size and type before a file can be used;
  - URLs are signed and expire after 15 minutes;
  - the storage keys use separate credentials from AWS monitoring.
- **Audit:** every admin and payment action is written to the audit log, plus per-enrollment history.

## Configuration (`backend/.env`; see `.env.example`)

| Variable | Purpose |
|---|---|
| `COURSE_API_TOKEN` | Enables the app API (32+ characters). Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `COURSE_ASSETS_BUCKET`, `COURSE_ASSETS_REGION`, `COURSE_ASSETS_PREFIX` | The S3 bucket for course files. Uploads are off until it's set; lecture video URLs work meanwhile. |
| `COURSE_ASSETS_ACCESS_KEY_ID`, `COURSE_ASSETS_SECRET_ACCESS_KEY` | A dedicated IAM user (or leave empty to use an instance role) with only `s3:PutObject` + `s3:GetObject` on `arn:aws:s3:::<bucket>/course-assets/*` |
| `COURSE_ASSETS_URL_TTL_SECONDS` (900), `COURSE_ASSET_MAX_VIDEO_MB` (4096), `COURSE_ASSET_MAX_FILE_MB` (100) | Signed URL lifetime and upload limits |

**Bucket CORS** (needed for browser uploads and previews):

```json
[{"AllowedOrigins": ["https://<dashboard-host>", "http://localhost:5173"],
  "AllowedMethods": ["POST", "GET"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3000}]
```

The dashboard's Content-Security-Policy adds the bucket's origin automatically when `COURSE_ASSETS_BUCKET` is set.

## Deployment and migration

1. Back up the database (`manage.sh backup`, or copy `data/monitor.db`).
2. `.\venv\Scripts\python -m alembic upgrade head` applies `0008` (course tables) and `0009` (AdhyaapakAI alignment: language codes, `student_name` dropped). `0009` stops with a clear message if a lecture uses Bhojpuri, Urdu or Assamese, which the app doesn't support. `alembic downgrade 0007` drops only the course tables.
3. Set the variables above, then restart the backend.
4. Give the app backend `COURSE_API_TOKEN`, and have it call the flow above after verifying payments.

## Limitations and pending work

- **The student app isn't wired up yet.** The AdhyaapakAI app still has to call the app API (catalog, orders, payment confirm, access, play, download). That's a change to the production app and needs your approval.
- **Payment verification is the app backend's job.** The dashboard trusts the caller holding `COURSE_API_TOKEN`; it doesn't talk to Razorpay or Google Play itself.
- **No pre-orders.** A future launch date blocks purchases until launch, and the course stays out of the catalog until then.
- **Uploads use a single presigned POST**, with no resumable multipart. S3 allows up to 5 GB per POST; the default limit is 4 GB.
- **No video transcoding or adaptive streaming.** Files are served as uploaded. Signed URLs last 15 minutes, so the player must request a new one after that.
- **Unused uploaded files aren't deleted** from the bucket automatically.
- **Expired status is written lazily**, on the next read. Access is always enforced on the expiry time, so this never delays the cut-off.
- **Refunds and cancellations** come from the app backend (`/payments/reverse`), or the owner can cancel in the dashboard. There is no refund button that moves money.

## Alignment with the AdhyaapakAI course contract

The course API follows the contract written for the AdhyaapakAI backend, so a later move of
course storage there (or the app reading these courses) needs no data reshaping:

| Topic | Contract | This dashboard |
|---|---|---|
| Money | Integer paise | `price_paise`, `original_price_paise`, `discounted_price_paise`, `effective_price_paise`; enrollments `list_price_paise`, `amount_paid_paise`; revenue `amount_paise`; app API input `amount_paid_paise` |
| Timestamps | ISO 8601 UTC with `Z` | Same (input must carry an offset, e.g. `+05:30`) |
| Languages | `hinglish`, `english`, `hindi`, `marathi`, `bengali`, `odia`, `gujarati`, `tamil`, `malayalam`, `kannada`, `telugu`, `punjabi` | Same codes |
| Student data on enrollments | Opaque user id only | Same (`student_id`) |
| S3 keys | `course-assets/{course_id}/{asset_id}-{filename}`, never `videos/` or `notes/` | Same (filename slugged) |
| Stale `version` on update | `409`, code `VERSION_CONFLICT` | Same |
| Delete | Drafts with no enrollments only | Same |
| Publish blocking | Title, price, a playable published lecture | Same |
| Page size | `limit` ≤ 500 | Same |
| Admin path | `/api/admin/courses/...` | `/api/v1/courses/...`. The sub-paths are identical, so it's one base-path constant (`frontend/src/pages/courses/api.ts`). The dashboard's `/api/v1/admin/*` namespace is owner-only, but course reads are for viewers too. |

**Still different** (kept because they are this dashboard's conventions):
- **IDs:** integers here; MongoDB ObjectId strings there.
- **Admin authentication:** dashboard session + CSRF (owner/viewer) here; the shared `x-admin-secret` header there.
- **Error body:** `{"error": {"code", "message", "request_id", "details"}}` here; `{"detail", "code"}` there. Codes are the dashboard's lowercase ones, except `VERSION_CONFLICT`.
- **Student API:** server-to-server `/api/v1/integrations/courses` with `COURSE_API_TOKEN` here; the contract's `/api/v2/courses` uses the app's student login, which only the AdhyaapakAI backend can provide.
