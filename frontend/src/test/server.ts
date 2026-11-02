import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import {
  alertPage,
  OWNER,
  overview,
  paymentPage,
  providers,
  syncStatus,
  threshold,
  VIEWER,
} from "./fixtures";

const API = "*/api/v1";

/** Records every request so tests can assert on methods, paths, headers and bodies. */
export const requests: {
  method: string;
  url: URL;
  auth: string | null;
  csrf: string | null;
  credentials: string | null;
  body: unknown;
}[] = [];

/** The mock server's session (stands in for the HttpOnly cookie, which tests cannot see). */
export const mockSession: { role: "owner" | "viewer" | null; expired: boolean } = { role: null, expired: false };
export const CSRF = "csrf-test-token-0123456789";

function role(_request: Request): "owner" | "viewer" | null {
  return mockSession.expired ? null : mockSession.role;
}

const unauthorized = () =>
  HttpResponse.json(
    { error: { code: mockSession.expired ? "session_expired" : "unauthenticated", message: "Not signed in", request_id: "req-1" } },
    { status: 401 },
  );
const forbidden = (code = "forbidden") =>
  HttpResponse.json({ error: { code, message: "Not allowed", request_id: "req-2" } }, { status: 403 });

/** Default handlers enforce the same auth rules as the backend. */
export const handlers = [
  http.all(`${API}/*`, ({ request }) => {
    if (new URL(request.url).pathname === "/api/v1/auth/login") return undefined;
    if (!role(request)) return unauthorized();
    if (request.method !== "GET") {
      // Same rules as the backend: CSRF token on every write, owner role for mutations.
      if (request.headers.get("X-CSRF-Token") !== CSRF) return forbidden("csrf_failed");
      if (role(request) !== "owner" && !new URL(request.url).pathname.endsWith("/auth/logout")) return forbidden();
    }
    return undefined; // fall through to the specific handler
  }),
  http.post(`${API}/auth/login`, async ({ request }) => {
    const { token } = (await request.json()) as { token: string };
    const granted = token === OWNER ? "owner" : token === VIEWER ? "viewer" : null;
    if (!granted)
      return HttpResponse.json({ error: { code: "unauthenticated", message: "That token was not accepted", request_id: "r" } }, { status: 401 });
    mockSession.role = granted;
    mockSession.expired = false;
    return HttpResponse.json({ role: granted, can_write: granted === "owner", via: "session", csrf_token: CSRF, expires_at: null });
  }),
  http.post(`${API}/auth/logout`, () => {
    mockSession.role = null;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get(`${API}/auth/session`, ({ request }) =>
    HttpResponse.json({ role: role(request), can_write: role(request) === "owner", via: "session", csrf_token: CSRF, expires_at: null }),
  ),
  http.get(`${API}/admin/operations`, () =>
    HttpResponse.json({ status: "ok", problems: [], disk_free_mb: 50000, scheduler: "running" }),
  ),
  http.get(`${API}/settings`, () => HttpResponse.json({ display_timezone: "UTC", supported_currencies: ["EUR", "GBP", "INR", "USD"] })),
  http.get(`${API}/overview`, () => HttpResponse.json(overview)),
  http.get(`${API}/providers`, () => HttpResponse.json(providers)),
  http.get(`${API}/providers/:id`, ({ params }) => {
    const p = providers.find((x) => x.id === params.id);
    return p ? HttpResponse.json(p) : HttpResponse.json({ error: { code: "provider_not_found", message: "Provider not found", request_id: "r" } }, { status: 404 });
  }),
  http.get(`${API}/providers/:id/sync-status`, () => HttpResponse.json(syncStatus)),
  http.get(`${API}/providers/:id/health-checks`, () => HttpResponse.json([])),
  http.get(`${API}/providers/:id/usage-series`, ({ params }) =>
    HttpResponse.json({ provider: params.id, days: 30, since: "2026-09-15T00:00:00Z", series: [], month_to_date: [], latest: [] }),
  ),
  http.get(`${API}/providers/:id/snapshots`, () => HttpResponse.json({ kind: "balance", items: [], total: 0, limit: 500, offset: 0 })),
  http.get(`${API}/providers/:id/threshold`, ({ params }) =>
    params.id === "deepgram"
      ? HttpResponse.json(threshold())
      : HttpResponse.json({ error: { code: "threshold_not_configured", message: "No threshold", request_id: "r" } }, { status: 404 }),
  ),
  http.get(`${API}/payments`, () => HttpResponse.json(paymentPage)),
  http.get(`${API}/alerts`, () => HttpResponse.json(alertPage)),
  http.get(`${API}/aws/resources`, () => HttpResponse.json({ items: [], observed_at: null, is_stale: true })),
  http.get(`${API}/notifications/status`, () =>
    HttpResponse.json({
      email_delivery_enabled: false,
      smtp_configured: true,
      smtp_host: "smtp.example.com",
      smtp_port: 587,
      smtp_security: "starttls",
      smtp_from: "monitor@example.com",
      smtp_auth_configured: true,
      default_recipients: ["ops@example.com"],
      dashboard_url: null,
      exchange_rates_configured: false,
      exchange_rate_source: null,
      outbox: { pending: 0, sending: 0, sent: 3, failed: 1, suppressed: 2 },
    }),
  ),
  // The SSE stream is exercised separately; here it just stays quiet.
  http.get(`${API}/events`, () => new HttpResponse(new ReadableStream({ start() {} }), { headers: { "Content-Type": "text/event-stream" } })),
];

export const server = setupServer(...handlers);

// Record every request, including ones answered by per-test overrides.
server.events.on("request:start", async ({ request }) => {
  const entry = {
    method: request.method,
    url: new URL(request.url),
    auth: request.headers.get("Authorization"),
    csrf: request.headers.get("X-CSRF-Token"),
    credentials: request.credentials ?? null,
    body: null as unknown,
  };
  requests.push(entry);
  try {
    const text = await request.clone().text();
    entry.body = text ? JSON.parse(text) : null;
  } catch {
    entry.body = null;
  }
});
