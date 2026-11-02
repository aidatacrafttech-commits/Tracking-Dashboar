# API Cost Monitor — dashboard UI

The dashboard is built with React 19, TypeScript and Vite. Data comes through TanStack Query and charts are drawn with Recharts.
It talks only to the backend under `/api/v1`; it never calls a provider directly.

## Run

```powershell
# 1. Backend (from backend/), one worker
.\venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100 --workers 1

# 2a. Development UI with hot reload: http://localhost:5173 (proxies /api to :8100)
cd frontend; npm install; npm run dev

# 2b. Production: build once; the backend then serves it at http://127.0.0.1:8100/
npm run build
```

Sign in with `DASHBOARD_OWNER_TOKEN` (read and write) or `DASHBOARD_VIEWER_TOKEN` (read-only) from `backend/.env`. The token is exchanged once for an HttpOnly session cookie; nothing is stored in browser storage, and writes carry a CSRF token kept in memory.

## Pages

| Page | Contents |
|---|---|
| Overview | Counts, last sync, AWS month-to-date and forecast, provider tiles, spend charts (one per currency), usage charts, recent payments and alerts |
| Providers / detail | Balance and its source, freshness, plan quota, threshold headroom, current-period usage, history charts, payments, sync status with safe errors, health, capabilities, manual balance entry |
| Payments | Search, provider/currency/date filters, sorting, pagination, totals per currency. Add, edit and delete (manual entries only, with confirmation and an audit trail), plus an explicit **Apply to balance** step |
| Thresholds | Per-provider settings with validation, email delivery status, test email, email preview and "Check now" |
| Alerts | Time, provider, balance, threshold, source, delivery status, recipients, retries; detail view with every attempt and the rendered email |
| AWS | Infrastructure health (EC2, EBS, S3; live API) kept separate from billing (Cost Explorer; delayed) |

## Live refresh

- **Change stream:** `GET /api/v1/events` is a Server-Sent Events stream read with `fetch` using the same-origin session cookie, so no credential ever appears in the URL. It carries topic names only, and the matching queries are refetched through the normal API.
- **Polling fallback:** queries also refetch every `VITE_POLL_INTERVAL_SECONDS` (default 60).
- **Hidden tab:** both polling and the stream stop while the tab is hidden, and the page catches up when it becomes visible.
- **Reconnects:** the stream reconnects with exponential backoff, capped at 30 s.

## Tests

```powershell
npm test            # Vitest + Testing Library + MSW (unit, integration, axe-core)
npm run lint        # ESLint incl. jsx-a11y (strict)
npm run typecheck
npm run e2e         # Playwright, installed Chrome, desktop / tablet / mobile
```

**How `npm run e2e` works**
- `e2e/serve_e2e.py` starts the **real** backend on a throwaway SQLite database filled with clearly labelled fixture data.
- Polling is disabled, so no provider is contacted.
- The backend serves the built UI.
- Screenshots are written to `e2e/screenshots/`.
- Run `npm run build` first.

**Conventions**
- Money values are decimal strings, formatted directly with `Intl.NumberFormat`. The browser never does money arithmetic; totals and headroom are computed by the backend.
- Charts convert values to numbers only for drawing. Tooltips and data tables show the exact strings.
