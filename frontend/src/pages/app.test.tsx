import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { axeViolations } from "../test/a11y";
import { OWNER, payment, paymentPage, provider, threshold } from "../test/fixtures";
import { renderApp } from "../test/render";
import { CSRF, mockSession, requests, server } from "../test/server";

describe("authentication", () => {
  it("shows only the sign-in page without a session, and never calls private APIs", async () => {
    renderApp("/payments", null);
    expect(await screen.findByRole("heading", { name: "API Cost Monitor" })).toBeInTheDocument();
    expect(screen.getByLabelText("Dashboard token")).toHaveAttribute("type", "password");
    expect(requests.filter((r) => r.url.pathname !== "/api/v1/auth/session")).toHaveLength(0);
  });

  it("rejects a wrong token, signs in with the right one, and stores no credential", async () => {
    const { user } = renderApp("/", null);
    await user.type(await screen.findByLabelText("Dashboard token"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That token was not accepted.");

    await user.clear(screen.getByLabelText("Dashboard token"));
    await user.type(screen.getByLabelText("Dashboard token"), OWNER);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Overview", level: 1 })).toBeInTheDocument();
    const loginReq = requests.find((r) => r.url.pathname === "/api/v1/auth/login" && r.method === "POST")!;
    expect(loginReq.body).toEqual({ token: "wrong-token", remember: false });
    // The token went to the server once; nothing is kept in browser storage.
    expect(Object.keys(localStorage)).toHaveLength(0);
    expect(Object.keys(sessionStorage)).toHaveLength(0);
    expect(requests.every((r) => r.auth === null)).toBe(true);
  });

  it("explains rate limiting", async () => {
    server.use(http.post("*/api/v1/auth/login", () =>
      HttpResponse.json({ error: { code: "too_many_attempts", message: "Too many", request_id: "r" } }, { status: 429 })));
    const { user } = renderApp("/", null);
    await user.type(await screen.findByLabelText("Dashboard token"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many failed attempts");
  });

  it("returns to sign-in with a notice when the session expires", async () => {
    renderApp("/");
    await screen.findByRole("heading", { name: "Overview", level: 1 });
    mockSession.expired = true;
    server.use(http.get("*/api/v1/overview", () =>
      HttpResponse.json({ error: { code: "session_expired", message: "expired", request_id: "r" } }, { status: 401 })));
    await screen.findByRole("button", { name: "Refresh" }).then((b) => b.click());
    expect(await screen.findByText("Your session expired. Please sign in again.")).toBeInTheDocument();
  });

  it("signs out through the server", async () => {
    const { user } = renderApp("/");
    await screen.findByRole("heading", { name: "Overview", level: 1 });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You have signed out.")).toBeInTheDocument();
    const logout = requests.find((r) => r.url.pathname === "/api/v1/auth/logout")!;
    expect(logout.method).toBe("POST");
    expect(logout.csrf).toBe(CSRF);
  });
});

describe("overview", () => {
  it("shows counts, per-currency figures and stale indicators", async () => {
    renderApp("/");
    const low = await screen.findByRole("region", { name: "Low balance" });
    expect(within(low).getByText("1")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "AWS spend this month" })).toHaveTextContent("45.10");
    // separate charts per currency, never a mixed total
    expect(screen.getByRole("heading", { name: "Spending trend (USD)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Spending trend (INR)" })).toBeInTheDocument();
    expect(screen.queryByText(/total spend/i)).not.toBeInTheDocument();
    // Deepgram tile: low balance + headroom; Sarvam tile: stale manual balance
    const deepgram = screen.getByRole("link", { name: /Deepgram/ });
    expect(deepgram).toHaveTextContent("Low balance");
    expect(deepgram).toHaveTextContent("−$6.60");
    expect(deepgram).toHaveTextContent("Failing: usage");
    const sarvam = screen.getByRole("link", { name: /Sarvam AI/ });
    expect(sarvam).toHaveTextContent(/Stale/);
    expect(sarvam).toHaveTextContent("Entered manually");
  });

  it("shows an error state with retry", async () => {
    server.use(http.get("*/api/v1/overview", () => HttpResponse.json({ error: { code: "internal_error", message: "An unexpected error occurred", request_id: "req-500" } }, { status: 500 })));
    const { user } = renderApp("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("An unexpected error occurred");
    expect(screen.getByText("req-500")).toBeInTheDocument();
    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "Low balance" })).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    renderApp("/");
    await screen.findByRole("region", { name: "Low balance" });
    expect(await axeViolations()).toEqual([]);
  });
});

describe("payments", () => {
  it("lists payments with per-currency totals and filters via the API", async () => {
    const { user } = renderApp("/payments");
    expect(await screen.findByText("INV-1")).toBeInTheDocument();
    const totals = screen.getByLabelText("Net totals for the current filters");
    expect(totals).toHaveTextContent("2,000.00");
    expect(totals).toHaveTextContent("250.50");

    await user.selectOptions(screen.getByLabelText("Provider"), "sarvam");
    await waitFor(() => expect(requests.some((r) => r.url.pathname === "/api/v1/payments" && r.url.searchParams.get("provider") === "sarvam")).toBe(true));
    await user.type(screen.getByLabelText("Search"), "upi");
    await waitFor(() => expect(requests.some((r) => r.url.searchParams.get("q") === "upi")).toBe(true), { timeout: 2000 });
    await user.click(screen.getByRole("button", { name: "Amount" }));
    await waitFor(() => expect(requests.some((r) => r.url.searchParams.get("sort") === "amount")).toBe(true));
  });

  it("keeps provider-verified payments read-only", async () => {
    renderApp("/payments");
    await screen.findByText("order-9");
    expect(screen.getAllByText("Read-only")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Edit payment INV-1" })).toBeInTheDocument();
  });

  it("hides every mutation from a viewer", async () => {
    renderApp("/payments", "viewer");
    await screen.findByText("INV-1");
    expect(screen.queryByRole("button", { name: /Record payment/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete payment/ })).not.toBeInTheDocument();
  });

  it("confirms before deleting and sends DELETE only after confirmation", async () => {
    let deleted = false;
    server.use(http.delete("*/api/v1/payments/1", () => {
      deleted = true;
      return new HttpResponse(null, { status: 204 });
    }));
    const { user } = renderApp("/payments");
    await user.click(await screen.findByRole("button", { name: "Delete payment INV-1" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete this payment?" });
    expect(dialog).toHaveTextContent("audit log");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(deleted).toBe(false);

    await user.click(screen.getByRole("button", { name: "Delete payment INV-1" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Delete this payment?" })).getByRole("button", { name: "Delete payment" }));
    await waitFor(() => expect(deleted).toBe(true));
    expect(requests.find((r) => r.method === "DELETE")!.csrf).toBe(CSRF);
    expect(await screen.findByText(/Payment deleted/)).toBeInTheDocument();
  });

  it("validates the payment form and sends amounts as strings", async () => {
    const { user } = renderApp("/payments");
    await user.click(await screen.findByRole("button", { name: /Record payment/ }));
    const dialog = await screen.findByRole("dialog", { name: "Record a payment" });
    await user.selectOptions(within(dialog).getByLabelText("Provider"), "openai");
    await user.type(within(dialog).getByLabelText("Amount"), "10.555");
    await user.click(within(dialog).getByRole("button", { name: "Record payment" }));
    expect(await within(dialog).findByText(/at most 2 decimals/)).toBeInTheDocument();
    expect(requests.some((r) => r.method === "POST")).toBe(false);

    server.use(http.post("*/api/v1/providers/openai/payments", async ({ request }) => HttpResponse.json(payment({ id: 9, ...(await request.json() as object) }), { status: 201 })));
    await user.clear(within(dialog).getByLabelText("Amount"));
    await user.type(within(dialog).getByLabelText("Amount"), "10.50");
    await user.click(within(dialog).getByRole("button", { name: "Record payment" }));
    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST")!;
    expect((post.body as { amount: unknown }).amount).toBe("10.50");
    expect(await screen.findByText(/does not change any balance/)).toBeInTheDocument();
  });

  it("applies a payment to the balance only through an explicit, previewed confirmation", async () => {
    server.use(
      http.get("*/api/v1/payments", () => HttpResponse.json({ ...paymentPage, items: [paymentPage.items[0]] })),
      http.get("*/api/v1/payments/1/reconcile-preview", () =>
        HttpResponse.json({ payment_id: 1, allowed: true, reason: null, base: { snapshot_id: 77, amount: "20", unit: "USD", source: "manual", observed_at: "2026-10-09T00:00:00Z" }, payment_effect: "50", resulting_balance: "70", unit: "USD" }),
      ),
      http.post("*/api/v1/payments/1/reconcile", () => HttpResponse.json({ id: 99, amount: "70", unit: "USD" })),
    );
    // OpenAI is a manual-balance provider in the fixtures, so the action is offered.
    const { user } = renderApp("/payments");
    await user.click(await screen.findByRole("button", { name: "Apply to balance" }));
    const dialog = await screen.findByRole("dialog", { name: "Apply payment to balance" });
    expect(await within(dialog).findByText("$70.00")).toBeInTheDocument();
    expect(requests.some((r) => r.url.pathname.endsWith("/reconcile"))).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Apply to balance" }));
    await waitFor(() => expect(requests.find((r) => r.url.pathname.endsWith("/reconcile"))?.body).toEqual({ base_snapshot_id: 77 }));
  });

  it("has no detectable accessibility violations", async () => {
    renderApp("/payments");
    await screen.findByText("INV-1");
    expect(await axeViolations()).toEqual([]);
  });
});

describe("thresholds", () => {
  it("validates input before saving", async () => {
    const { user } = renderApp("/thresholds");
    const amount = await screen.findByLabelText("Alert when balance is at or below (USD)", {}, { timeout: 3000 }).catch(() => null);
    const field = amount ?? (await screen.findAllByLabelText(/Alert when balance is at or below \(USD\)/))[0]!;
    await user.clear(field);
    await user.type(field, "abc");
    const form = field.closest("form")!;
    await user.click(within(form).getByRole("button", { name: "Save threshold" }));
    expect(await within(form).findByText("Enter an amount like 25.00.")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(requests.some((r) => r.method === "PUT")).toBe(false);
  });

  it("saves and reports the evaluation result", async () => {
    server.use(http.put("*/api/v1/providers/deepgram/threshold", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json(threshold({ amount: body.amount as string, version: 4 }));
    }));
    const { user } = renderApp("/thresholds");
    const field = (await screen.findAllByLabelText(/Alert when balance is at or below \(USD\)/))[0]!;
    await user.clear(field);
    await user.type(field, "30.00");
    await user.click(within(field.closest("form")!).getByRole("button", { name: "Save threshold" }));
    expect(await screen.findByText(/threshold saved/)).toBeInTheDocument();
    const put = requests.find((r) => r.method === "PUT")!;
    expect(put.body).toMatchObject({ amount: "30.00", unit: "USD", eligible_sources: ["api_verified", "manual"], notification_emails: null });
  });

  it("warns that email delivery is off and sends a test email", async () => {
    server.use(http.post("*/api/v1/notifications/test-email", () => HttpResponse.json({ event_id: 5, recipients: ["me@example.com"], delivery_status: "sent", error_code: null, error_message: null })));
    const { user } = renderApp("/thresholds");
    expect(await screen.findByText(/Alert emails are/)).toHaveTextContent("off");
    await user.type(screen.getByLabelText("Send a test email to"), "me@example.com");
    await user.click(screen.getByRole("button", { name: "Send test email" }));
    expect(await screen.findByText("Test email sent to me@example.com.")).toBeInTheDocument();
  });
});

describe("alerts", () => {
  it("shows delivery status and offers retry only for failed alerts", async () => {
    const { user } = renderApp("/alerts");
    expect(await screen.findAllByText("Verified by provider API")).toHaveLength(2);
    const table = screen.getByRole("table", { name: "Alert history" });
    expect(within(table).getByText("Sent")).toBeInTheDocument();
    expect(within(table).getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Gave up after 6 attempts")).toBeInTheDocument();
    const retries = screen.getAllByRole("button", { name: "Retry" });
    expect(retries).toHaveLength(1);
    server.use(http.post("*/api/v1/alerts/11/retry", () => HttpResponse.json({})));
    await user.click(retries[0]!);
    expect(await screen.findByText("Alert re-queued for delivery.")).toBeInTheDocument();
  });

  it("does not offer retry to a viewer", async () => {
    renderApp("/alerts", "viewer");
    await screen.findByRole("table", { name: "Alert history" });
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});

describe("AWS page", () => {
  it("separates resource health from billing and explains disabled monitoring", async () => {
    server.use(http.get("*/api/v1/providers/aws", () => HttpResponse.json(provider({ id: "aws", display_name: "AWS", integration: { status: "disabled", missing_config: [], note: "set AWS_MONITORING_ENABLED=true", capabilities: [] } }))));
    renderApp("/aws");
    expect(await screen.findByRole("heading", { name: "Infrastructure health" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Billing" })).toBeInTheDocument();
    expect(screen.getByText(/not a live balance/)).toBeInTheDocument();
    expect(screen.getByText(/AWS monitoring is off/)).toBeInTheDocument();
  });
});

describe("provider detail", () => {
  it("shows balance source, threshold headroom and safe sync errors", async () => {
    renderApp("/providers/deepgram");
    expect(await screen.findByRole("heading", { name: "Deepgram", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText("−$6.60")).toBeInTheDocument();
    expect(screen.getAllByText("Verified by provider API").length).toBeGreaterThan(0);
    expect(await screen.findByText("rate limited by provider (429)")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(OWNER);
  });

  it("shows a not-found state for unknown providers", async () => {
    renderApp("/providers/nope");
    expect(await screen.findByText('No provider "nope"')).toBeInTheDocument();
  });
});

describe("live updates", () => {
  it("refetches only the affected data when the server reports a change", async () => {
    // The test decides when the server reports the change (after counting the first
    // load), so a slow first render can't race the event.
    let reportChange: () => void = () => undefined;
    server.use(
      http.get("*/api/v1/events", () => {
        const enc = new TextEncoder();
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(enc.encode('event: ready\ndata: {"topics":[]}\n\n'));
            reportChange = () => controller.enqueue(enc.encode('event: changed\ndata: {"topics":["payments"]}\n\n'));
          },
        });
        return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    renderApp("/payments");
    await screen.findByText("INV-1");
    expect(await screen.findByText("Live updates on")).toBeInTheDocument();
    const payments = () => requests.filter((r) => r.url.pathname === "/api/v1/payments").length;
    const before = payments();
    reportChange();
    await waitFor(() => expect(payments()).toBeGreaterThan(before), { timeout: 3000 });
    // the stream rides on the session cookie: no token in a header or the URL
    const stream = requests.find((r) => r.url.pathname === "/api/v1/events")!;
    expect(stream.auth).toBeNull();
    expect(stream.url.search).toBe("");
    expect(await screen.findByText("Live updates on")).toBeInTheDocument();
  });
});
