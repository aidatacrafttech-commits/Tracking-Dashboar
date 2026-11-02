# Voucher Management (AdhyaapakAI backend)

**Voucher Management** in the dashboard sidebar is an admin UI for the **AdhyaapakAI backend's voucher API**. Vouchers, subjects, packages and redemptions all live in AdhyaapakAI; this dashboard stores none of them.

| Tab | What it does |
|---|---|
| Course vouchers | List, filter, create, edit, activate/deactivate, delete; view redemptions |
| Live Tutor vouchers | The same, for the Beginner / Intermediate / Pro packages |
| Redemptions | Who used which code and for what, across both categories: code, user ID, subject or package, price − discount = paid, status, order and payment. Each row shows the user's name ("Deleted account" if the account is gone), email and phone, each with a copy button. Searchable by code, name, email, phone or order ID, with **Export CSV**. The API lists redemptions per voucher, so this tab reads each used voucher's list. Email and phone are admin-only data: they are only shown, copied to the clipboard on request, or exported on request, and never stored or logged. |
| Subjects | Add custom subjects; rename, activate or deactivate any subject; delete custom ones |
| Packages | Edit a package's name, price, tokens, status and sort order, and link a token pack |

## Connecting

**Development**

The browser only calls relative `/api/...` URLs on the dashboard's own origin. The Vite dev server forwards them on (`frontend/vite.config.ts`):

| Path | Goes to | Override |
|---|---|---|
| `/api/v1/...` | this dashboard's backend, `http://127.0.0.1:8100` | `DASHBOARD_BACKEND_URL` |
| `/api/...` (everything else) | AdhyaapakAI, **live** `https://api.adhyaapak.ai` (local `http://127.0.0.1:8001` is kept commented out in `frontend/.env`) | `ADHYAAPAK_BACKEND_URL` |

This is needed because AdhyaapakAI's CORS only allows the app's own origins. The backend's CORS configuration is not changed.

`VITE_ADHYAAPAK_API_BASE` (default `/api`) is the base path the UI calls. It is not secret.

**Production (not set up yet)**

The deployed dashboard serves the built UI from its own FastAPI. For Voucher Management to work there, the reverse proxy (nginx) needs a rule forwarding `/api/vouchers/` to `https://api.adhyaapak.ai/api/vouchers/`.

Pointing `VITE_ADHYAAPAK_API_BASE` straight at `https://api.adhyaapak.ai/api` would not work: AdhyaapakAI's CORS doesn't allow the dashboard's origin, and the dashboard's CSP (`connect-src 'self'`) blocks other origins.

The deployment files have not been changed; that needs your go-ahead.

## Admin secret

- Every request sends `x-admin-secret`. The admin types the secret on the **Unlock Voucher Management** screen.
- The dashboard checks the secret with one read-only call (`GET /vouchers/subjects`) before showing anything.
- The secret is kept in memory and this tab's `sessionStorage` only. It is never in source code, `.env` files or the build. It is cleared by **Sign out of voucher admin**, or when the tab closes.
- Any `403 {"detail":"Forbidden."}` clears it and goes back to the secret screen.
- Requests use `credentials: "omit"`, so the dashboard's own login cookie is never sent to AdhyaapakAI.
- Dashboard roles (owner/viewer) don't limit voucher admin. Anyone with the AdhyaapakAI admin secret has full voucher admin rights.

## Endpoints used

All paths are under `/api`. `{prefix}` is `courses` or `live-tutor`.

| Action | Request |
|---|---|
| List | `GET /vouchers/{prefix}?status=&q=&include_deleted=&limit=50&skip=` |
| Create | `POST /vouchers/{prefix}` |
| Edit | `PATCH /vouchers/{prefix}/{id}`, with only the changed fields; `null` clears a field |
| Activate / deactivate | `POST /vouchers/{prefix}/{id}/activate` · `/deactivate` |
| Delete | `DELETE /vouchers/{prefix}/{id}` (soft delete) |
| Redemptions | `GET /vouchers/{prefix}/{id}/redemptions?limit=50&skip=` |
| Subjects | `GET/POST /vouchers/subjects`, `PATCH/DELETE /vouchers/subjects/{key}` |
| Packages | `GET /vouchers/live-tutor/packages/all`, `PUT /vouchers/live-tutor/packages/{key}` |

**Form rules**
- Optional numbers left empty are not sent: never `0` and never `""`.
- `max_discount_inr` is only sent for percent discounts.
- Dates are entered in IST and sent with `+05:30`.
- At least one subject or package is required unless *All* is ticked.
- The code field is locked after the voucher's first redemption.
- An edit with no changes sends nothing.

**Errors**
- `detail` text from the backend is shown as-is.
- 422 lists are mapped to the field named in `loc`. For example, `subject_keys` and `package_keys` are shown under the picker.

All dates are shown in IST (Asia/Kolkata).

## Caution: real data

Since 2026-10-23 the dev proxy points at the **live** AdhyaapakAI backend (`https://api.adhyaapak.ai`). Every create, edit, activate/deactivate, delete, subject or package change in this UI changes **production** vouchers that real app users redeem. Don't make test changes there. To work against a laptop backend instead, switch the commented `ADHYAAPAK_BACKEND_URL` line in `frontend/.env` and restart the dev server.

The automated tests never call it: they use mocks. The only live calls made while building this were requests without a secret, or with a deliberately wrong one, which the backend refused.

## The dashboard's earlier, built-in voucher module

Before this change, Voucher Management used the dashboard's own voucher backend: `/api/v1/vouchers`, migrations `0006`/`0007`, and the `VOUCHER_API_TOKEN` integration.

- The UI no longer uses it. Its backend routes, tables and tests are still in place, and its tables are empty.
- The old UI code is archived in `backend/backups/local-voucher-ui-20261022.zip`.
- Whether to remove the old backend module is your decision.
