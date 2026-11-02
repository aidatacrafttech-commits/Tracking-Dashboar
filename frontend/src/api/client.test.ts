import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { CSRF, mockSession, requests, server } from "../test/server";
import { ApiError, apiFetch, buildUrl, csrfStore, onUnauthorized } from "./client";

describe("api client", () => {
  it("uses the same-origin session cookie and never an Authorization header", async () => {
    mockSession.role = "owner";
    await apiFetch("/providers");
    const [req] = requests;
    expect(req!.url.pathname).toBe("/api/v1/providers");
    expect(req!.auth).toBeNull();
    expect(req!.credentials).toBe("same-origin");
  });

  it("sends the CSRF token on writes only", async () => {
    mockSession.role = "owner";
    csrfStore.set(CSRF);
    server.use(http.delete("*/api/v1/payments/1", () => new HttpResponse(null, { status: 204 })));
    await apiFetch("/providers");
    await apiFetch("/payments/1", { method: "DELETE" });
    expect(requests[0]!.csrf).toBeNull();
    expect(requests[1]!.csrf).toBe(CSRF);
  });

  it("is refused by the server without a CSRF token", async () => {
    mockSession.role = "owner";
    csrfStore.set(null);
    await expect(apiFetch("/payments/1", { method: "DELETE" })).rejects.toMatchObject({ status: 403, code: "csrf_failed" });
  });

  it("stores no credential in browser storage", async () => {
    mockSession.role = "owner";
    csrfStore.set(CSRF);
    await apiFetch("/providers");
    expect(Object.keys(localStorage)).toHaveLength(0);
    expect(Object.keys(sessionStorage)).toHaveLength(0);
  });

  it("maps the error envelope and exposes field errors", async () => {
    mockSession.role = "owner";
    csrfStore.set(CSRF);
    server.use(
      http.post("*/api/v1/providers/openai/payments", () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "Request validation failed",
              request_id: "req-42",
              details: [{ loc: ["body", "amount"], msg: "must have at most 2 decimal places", type: "value_error" }],
            },
          },
          { status: 422 },
        ),
      ),
    );
    const err = (await apiFetch("/providers/openai/payments", { method: "POST", body: {} }).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.requestId).toBe("req-42");
    expect(err.fieldErrors()).toEqual({ amount: "must have at most 2 decimal places" });
  });

  it("notifies listeners on 401 with the reason", async () => {
    mockSession.role = "owner";
    mockSession.expired = true;
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(apiFetch("/overview")).rejects.toMatchObject({ status: 401, code: "session_expired" });
    expect(listener).toHaveBeenCalledWith("session_expired");
    off();
  });

  it("reports network failures without leaking details", async () => {
    mockSession.role = "owner";
    server.use(http.get("*/api/v1/overview", () => HttpResponse.error()));
    await expect(apiFetch("/overview")).rejects.toMatchObject({ code: "network_error", status: 0 });
  });

  it("builds query strings, skipping empty values", () => {
    expect(buildUrl("/payments", { q: "inv", provider: "", currency: undefined, limit: 25 })).toBe("/api/v1/payments?q=inv&limit=25");
  });
});
