import type { ApiErrorBody } from "./types";

// All provider data comes from our own backend under /api/v1 (same origin). The browser
// never talks to OpenAI, Deepgram, AWS etc. directly and never sees their credentials.
export const API_BASE = "/api/v1";

/**
 * Browser sessions use an HttpOnly cookie set by the server, which scripts cannot read,
 * so no credential is ever stored in localStorage/sessionStorage. Writes must echo the
 * session's CSRF token, which is kept only in memory and re-fetched after a reload.
 */
let csrfToken: string | null = null;

export const csrfStore = {
  get: () => csrfToken,
  set: (token: string | null) => {
    csrfToken = token;
  },
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, requestId: string | null, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  /** Field-level messages from a 422 validation envelope, keyed by field name. */
  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    if (Array.isArray(this.details)) {
      for (const item of this.details as { loc?: string[]; msg?: string }[]) {
        const field = item.loc?.[item.loc.length - 1];
        if (field && item.msg && !out[field]) out[field] = item.msg;
      }
    }
    return out;
  }
}

type UnauthorizedListener = (reason: string) => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Called whenever the API answers 401 (signed out, expired or revoked session). */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export function notifyUnauthorized(reason: string): void {
  unauthorizedListeners.forEach((l) => l(reason));
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export async function apiFetch<T>(
  path: string,
  init: { method?: string; body?: unknown; query?: Query; signal?: AbortSignal; quiet401?: boolean } = {},
): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  if (!SAFE.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, init.query), {
      method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
      credentials: "same-origin", // the session cookie; never sent to other origins
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Cannot reach the monitoring server.", null);
  }

  if (response.status === 204) return undefined as T;
  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const body = (payload as ApiErrorBody | null)?.error;
    const code = body?.code ?? "http_error";
    if (response.status === 401 && !init.quiet401) notifyUnauthorized(code);
    throw new ApiError(
      response.status,
      code,
      body?.message ?? `Request failed (${response.status})`,
      body?.request_id ?? response.headers.get("X-Request-ID"),
      body?.details,
    );
  }
  return payload as T;
}
