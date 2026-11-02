// Client for the AdhyaapakAI voucher admin API.
//
// The browser calls relative /api/... on this dashboard's origin; the dev server (or, in
// production, the reverse proxy) forwards it to the AdhyaapakAI backend, whose CORS only
// allows the app's own origins. Every request carries the x-admin-secret header.
//
// The admin secret is never in source, env files or the build: the admin types it in, and
// it lives in memory plus this tab's sessionStorage (cleared on sign-out or tab close).
// Requests use credentials: "omit" so this dashboard's own session cookie is never
// forwarded to the AdhyaapakAI backend.

import { useSyncExternalStore } from "react";
import { ApiError, type Query } from "../../api/client";

/** From frontend/.env (VITE_ADHYAAPAK_API_BASE, e.g. "/api"); not a secret. Empty = not configured. */
export const ADHYAAPAK_API_BASE = String(import.meta.env.VITE_ADHYAAPAK_API_BASE ?? "").trim().replace(/\/+$/, "");
const STORAGE_KEY = "adhyaapak-admin-secret";

function readStored(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

interface SecretState {
  secret: string | null;
  /** True after the backend refused the stored secret (403), so the gate can say why. */
  rejected: boolean;
}

let state: SecretState = { secret: readStored(), rejected: false };
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export const adminSecret = {
  get: () => state.secret,
  set(value: string) {
    state = { secret: value, rejected: false };
    try {
      sessionStorage.setItem(STORAGE_KEY, value);
    } catch {
      /* memory only */
    }
    emit();
  },
  clear(rejected = false) {
    state = { secret: null, rejected };
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* nothing stored */
    }
    emit();
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  snapshot: () => state,
};

export function useAdminSecret(): SecretState {
  return useSyncExternalStore(adminSecret.subscribe, adminSecret.snapshot);
}

/** A failed AdhyaapakAI call. `message` is the backend's `detail` text, shown as-is; for
 * 422 responses `details` is the validation list, so `fieldErrors()` maps it to fields. */
export class AdminApiError extends ApiError {}

function url(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return `${ADHYAAPAK_API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

export async function adminFetch<T>(
  path: string,
  init: { method?: string; body?: unknown; query?: Query; secret?: string } = {},
): Promise<T> {
  if (!ADHYAAPAK_API_BASE)
    throw new AdminApiError(0, "not_configured", "Set VITE_ADHYAAPAK_API_BASE in frontend/.env and restart the dev server.", null);
  const secret = init.secret ?? adminSecret.get();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (secret) headers["x-admin-secret"] = secret;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(url(path, init.query), {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new AdminApiError(0, "network_error", "Cannot reach the AdhyaapakAI backend. Is it running?", null);
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;
    // A wrong or missing secret: back to the secret screen (unless this call is the check
    // of a newly typed secret, which reports the problem itself).
    if (response.status === 403 && init.secret === undefined) adminSecret.clear(true);
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? "Some fields need attention."
          : `Request failed (${response.status})`;
    throw new AdminApiError(response.status, `http_${response.status}`, message, null, Array.isArray(detail) ? detail : undefined);
  }
  return payload as T;
}
