// Page-level behaviour not covered in app.test.tsx: loading/error states, provider cards and
// details, manual balance entry, server-side validation, alert details, authorization and
// the owner operations banner. Everything runs against the MSW backend in src/test/server.ts.

import { screen, waitFor, within } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { axeViolations } from "../test/a11y";
import { alert, alertPage, provider, providers } from "../test/fixtures";
import { renderApp } from "../test/render";
import { CSRF, requests, server } from "../test/server";
import { validateThreshold } from "./ThresholdsPage";

const serverError = () =>
  HttpResponse.json({ error: { code: "internal_error", message: "Internal server error", request_id: "req-500" } }, { status: 500 });

describe("loading and error states", () => {
  it("shows an accessible loading state while data is on its way", async () => {
    server.use(http.get("*/api/v1/providers", async () => {
      await delay(300);
      return HttpResponse.json(providers);
    }));
    renderApp("/providers");
    expect(await screen.findByRole("heading", { name: "Providers", level: 1 })).toBeInTheDocument();
    expect(screen.getAllByRole("status").some((s) => s.textContent?.includes("Loading"))).toBe(true);
    expect(await screen.findByRole("heading", { name: "Deepgram", level: 2 })).toBeInTheDocument();
  });

  it("shows a safe error with the request id and recovers on retry", async () => {
    let fail = true;
    server.use(http.get("*/api/v1/providers", () => (fail ? serverError() : HttpResponse.json(providers))));
    const { user } = renderApp("/providers");
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent("Could not load data.");
    expect(error).toHaveTextContent("req-500");
    fail = false;
    await user.click(within(error).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Deepgram", level: 2 })).toBeInTheDocument();
  });

  it("shows an error inside the alert details dialog when loading fails", async () => {
    server.use(http.get("*/api/v1/alerts/10", serverError));
    const { user } = renderApp("/alerts");
    await screen.findByRole("table", { name: "Alert history" });
    await user.click(screen.getAllByRole("button", { name: "Details" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Alert details" });
    expect(await within(dialog).findByText(/Could not load data/)).toBeInTheDocument();
  });
});

describe("provider cards", () => {
  it("shows balance, source, staleness, threshold and missing configuration for each provider", async () => {
    renderApp("/providers");
    const card = async (name: string) => (await screen.findByRole("heading", { name, level: 2 })).closest("a")!;

    const deepgram = await card("Deepgram");
    expect(deepgram).toHaveAttribute("href", "/providers/deepgram");
    expect(within(deepgram).getByText("$18.40")).toBeInTheDocument();
    expect(within(deepgram).getByText("Low balance")).toBeInTheDocument();
    expect(within(deepgram).getByText("−$6.60")).toBeInTheDocument();
    // Configured but a sync is failing: the badge says so instead of "Synced".
    expect(await within(deepgram).findByText("Failing: usage (rate limited)")).toBeInTheDocument();
    expect(within(deepgram).queryByText("Synced")).not.toBeInTheDocument();
    expect(within(deepgram).getByText("Spend this month")).toBeInTheDocument();
    expect(within(deepgram).getByText("$12.50")).toBeInTheDocument();

    const openai = await card("OpenAI");
    expect(within(openai).getByText("Unknown")).toBeInTheDocument(); // never shown as zero
    expect(within(openai).getByText("Needs: OPENAI_ADMIN_KEY")).toBeInTheDocument();
    expect(within(openai).getByText("Not set")).toBeInTheDocument();
    // No balance API: the card says how to get a balance in, instead of a bare "No data".
    expect(within(openai).getByText(/No balance API: open this card and use/)).toBeInTheDocument();

    const sarvam = await card("Sarvam AI");
    expect(within(sarvam).getByText("Synced")).toBeInTheDocument(); // healthy: no failing operation
    // Every card has the row; Sarvam has no usage/billing API, so it says so instead of a number.
    expect(within(sarvam).getByText("Spend this month")).toBeInTheDocument();
    expect(within(sarvam).getByText("Not reported by provider")).toBeInTheDocument();
    expect(within(sarvam).getByText(/2,500\.00/)).toBeInTheDocument();
    expect(within(sarvam).getByText("Entered manually")).toBeInTheDocument();
    expect(within(sarvam).getByText(/stale/i)).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = renderApp("/providers");
    await screen.findByRole("heading", { name: "Deepgram", level: 2 });
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("provider details", () => {
  it("warns that a stale balance is not used for alerts", async () => {
    renderApp("/providers/sarvam");
    expect(await screen.findByRole("heading", { name: "Sarvam AI", level: 1 })).toBeInTheDocument();
    expect(await screen.findByText(/older than 120 minutes, so it is shown for reference only and not used for alerts/)).toBeInTheDocument();
  });

  it("explains a missing integration without showing any secret", async () => {
    renderApp("/providers/openai");
    expect(await screen.findByText(/This integration is not configured/)).toBeInTheDocument();
    expect(screen.getByText("OPENAI_ADMIN_KEY")).toBeInTheDocument();
    expect(screen.getByText("No balance recorded yet")).toBeInTheDocument();
  });

  it("validates and records a manual balance with the CSRF token", async () => {
    server.use(http.post("*/api/v1/providers/sarvam/balance-entries", () => HttpResponse.json({}, { status: 201 })));
    const { user } = renderApp("/providers/sarvam");
    const field = await screen.findByLabelText("Balance (INR)");
    await user.type(field, "1,000");
    await user.click(screen.getByRole("button", { name: "Save balance" }));
    expect(await screen.findByText("Enter an amount like 42.50 (INR).")).toBeInTheDocument();
    expect(requests.some((r) => r.method === "POST")).toBe(false);

    await user.clear(field);
    await user.type(field, "1000.50");
    await user.type(screen.getByLabelText("Note (optional)"), "from Sarvam dashboard");
    await user.click(screen.getByRole("button", { name: "Save balance" }));
    expect(await screen.findByText(/Recorded .*1,000\.50 for Sarvam AI\./)).toBeInTheDocument();
    const post = requests.find((r) => r.method === "POST" && r.url.pathname.endsWith("/balance-entries"))!;
    expect(post.body).toEqual({ amount: "1000.50", unit: "INR", notes: "from Sarvam dashboard" });
    expect(post.csrf).toBe(CSRF);
  });

  it("shows the server's message when a manual balance is refused", async () => {
    server.use(http.post("*/api/v1/providers/sarvam/balance-entries", () =>
      HttpResponse.json({ error: { code: "invalid_precision", message: "amount must have at most 2 decimal places", request_id: "r" } }, { status: 422 })));
    const { user } = renderApp("/providers/sarvam");
    await user.type(await screen.findByLabelText("Balance (INR)"), "10.50");
    await user.click(screen.getByRole("button", { name: "Save balance" }));
    expect(await screen.findByText("amount must have at most 2 decimal places")).toBeInTheDocument();
  });

  it("lets the owner sync now and reports a failed sync safely", async () => {
    server.use(http.post("*/api/v1/providers/deepgram/sync", () =>
      HttpResponse.json({
        provider: "deepgram", skipped: false, reason: null, run_id: 7, status: "failed", records_written: 0,
        operations: [{ operation: "balance", status: "error", error_code: "auth_failed", error_message: "credentials rejected", warnings: [] }],
      })));
    const { user } = renderApp("/providers/deepgram");
    await user.click(await screen.findByRole("button", { name: /Sync now/ }));
    expect(await screen.findByText("Sync failed: balance auth_failed")).toBeInTheDocument();
  });

  it("gives a viewer no sync button and no manual entry form", async () => {
    renderApp("/providers/sarvam", "viewer");
    await screen.findByRole("heading", { name: "Sarvam AI", level: 1 });
    await screen.findByRole("table", { name: "Sync status by operation" });
    expect(screen.queryByRole("button", { name: /Sync now/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Balance (INR)")).not.toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = renderApp("/providers/deepgram");
    await screen.findByRole("table", { name: "Sync status by operation" });
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("threshold editing", () => {
  const form = {
    amount: "25.00",
    is_enabled: true,
    notify_on_breach: true,
    notify_on_recovery: true,
    recovery_margin: "5",
    cooldown_minutes: "",
    monitoring_interval_minutes: "",
    recipients: "",
    eligible_sources: ["api_verified" as const],
  };

  it("validates every field the way the server does", () => {
    expect(validateThreshold(form, "USD")).toEqual({});
    expect(validateThreshold({ ...form, amount: "25.001" }, "USD").amount).toBeDefined();
    expect(validateThreshold({ ...form, amount: "-1" }, "USD").amount).toBeDefined();
    expect(validateThreshold({ ...form, amount: "1.5" }, "CREDITS").amount).toBe("Enter a whole number of credits.");
    expect(validateThreshold({ ...form, recovery_margin: "x" }, "USD").recovery_margin).toBeDefined();
    expect(validateThreshold({ ...form, cooldown_minutes: "14" }, "USD").cooldown_minutes).toBeDefined();
    expect(validateThreshold({ ...form, cooldown_minutes: "10081" }, "USD").cooldown_minutes).toBeDefined();
    expect(validateThreshold({ ...form, cooldown_minutes: "60" }, "USD")).toEqual({});
    expect(validateThreshold({ ...form, monitoring_interval_minutes: "4" }, "USD").monitoring_interval_minutes).toBeDefined();
    expect(validateThreshold({ ...form, recipients: "ops@example.com, not-an-email" }, "USD").notification_emails).toBe('"not-an-email" is not a valid email address.');
    expect(validateThreshold({ ...form, recipients: Array.from({ length: 11 }, (_, i) => `a${i}@example.com`).join(",") }, "USD").notification_emails).toBe("At most 10 recipients.");
    expect(validateThreshold({ ...form, eligible_sources: [] }, "USD").eligible_sources).toBe("Choose at least one balance source.");
  });

  it("shows field errors returned by the server next to the field", async () => {
    server.use(http.put("*/api/v1/providers/deepgram/threshold", () =>
      HttpResponse.json({
        error: { code: "validation_error", message: "Request validation failed", request_id: "r", details: [{ loc: ["body", "amount"], msg: "must be greater than 0", type: "value_error" }] },
      }, { status: 422 })));
    const { user } = renderApp("/thresholds");
    const field = (await screen.findAllByLabelText(/Alert when balance is at or below \(USD\)/))[0]!;
    await user.clear(field);
    await user.type(field, "0");
    await user.click(within(field.closest("form")!).getByRole("button", { name: "Save threshold" }));
    expect(await screen.findByText("must be greater than 0")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");
  });

  it("shows a conflict when the threshold changed elsewhere", async () => {
    server.use(http.put("*/api/v1/providers/deepgram/threshold", () =>
      HttpResponse.json({ error: { code: "concurrent_modification", message: "Threshold was modified concurrently; reload and retry", request_id: "r" } }, { status: 409 })));
    const { user } = renderApp("/thresholds");
    const field = (await screen.findAllByLabelText(/Alert when balance is at or below \(USD\)/))[0]!;
    await user.click(within(field.closest("form")!).getByRole("button", { name: "Save threshold" }));
    expect(await screen.findByText("Threshold was modified concurrently; reload and retry")).toBeInTheDocument();
  });

  it("is read-only for a viewer", async () => {
    renderApp("/thresholds", "viewer");
    const field = (await screen.findAllByLabelText(/Alert when balance is at or below \(USD\)/))[0]!;
    expect(field).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save threshold" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send test email" })).not.toBeInTheDocument();
  });
});

describe("alert history", () => {
  it("shows the delivery attempts and renders the email in a sandboxed frame", async () => {
    server.use(http.get("*/api/v1/alerts/11", () =>
      HttpResponse.json({
        ...alertPage.items[1],
        attempts: [
          { attempt_number: 1, outcome: "retry", error_code: "unavailable", error_message: "server down", smtp_status: null, started_at: "2026-10-14T06:00:00Z", finished_at: "2026-10-14T06:00:01Z" },
          { attempt_number: 2, outcome: "failed", error_code: "unavailable", error_message: "server down", smtp_status: null, started_at: "2026-10-14T07:00:00Z", finished_at: "2026-10-14T07:00:01Z" },
        ],
        body_text: "Deepgram balance is low",
        body_html: "<p>Deepgram balance is low</p><script>parent.hacked = true</script>",
        context: null,
      })));
    const { user } = renderApp("/alerts");
    await screen.findByRole("table", { name: "Alert history" });
    await user.click(screen.getAllByRole("button", { name: "Details" })[1]!);
    const dialog = await screen.findByRole("dialog", { name: "Alert details" });
    const attempts = await within(dialog).findByRole("table", { name: "Delivery attempts" });
    expect(within(attempts).getAllByRole("row")).toHaveLength(3);
    expect(within(attempts).getByText("Failed")).toBeInTheDocument();
    const frame = within(dialog).getByTitle("Alert email content");
    expect(frame).toHaveAttribute("sandbox", ""); // no scripts, no same-origin access
    expect((window as unknown as { hacked?: boolean }).hacked).toBeUndefined();
  });

  it("explains why a suppressed alert was not emailed", async () => {
    server.use(
      http.get("*/api/v1/alerts", () => HttpResponse.json({ ...alertPage, items: [alert({ id: 12, delivery_status: "suppressed", delivery_attempts: 0, last_error: "email delivery disabled (EMAIL_DELIVERY_ENABLED=false)", sent_at: null })], total: 1 })),
      http.get("*/api/v1/alerts/12", () => HttpResponse.json({ ...alert({ id: 12, delivery_status: "suppressed", last_error: "email delivery disabled (EMAIL_DELIVERY_ENABLED=false)" }), body_text: null, body_html: null, context: null })),
    );
    const { user } = renderApp("/alerts");
    await user.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText("Not emailed: email delivery disabled (EMAIL_DELIVERY_ENABLED=false)")).toBeInTheDocument();
  });

  it("filters by provider and status through the API", async () => {
    const { user } = renderApp("/alerts");
    await screen.findByRole("table", { name: "Alert history" });
    await user.selectOptions(screen.getByLabelText("Provider"), "deepgram");
    await user.selectOptions(screen.getByLabelText("Delivery status"), "failed");
    await waitFor(() => {
      const last = requests.filter((r) => r.url.pathname === "/api/v1/alerts").at(-1)!;
      expect(last.url.searchParams.get("provider")).toBe("deepgram");
      expect(last.url.searchParams.get("status")).toBe("failed");
    });
  });

  it("shows an empty state when there are no alerts", async () => {
    server.use(http.get("*/api/v1/alerts", () => HttpResponse.json({ items: [], total: 0, limit: 25, offset: 0 })));
    renderApp("/alerts");
    expect(await screen.findByText("No alerts yet")).toBeInTheDocument();
  });
});

describe("authorization and operations", () => {
  it("shows the owner an operations banner when backups or disk need attention", async () => {
    server.use(http.get("*/api/v1/admin/operations", () =>
      HttpResponse.json({ status: "critical", problems: ["last backup failed", "disk space low"], disk_free_mb: 120, scheduler: "running" })));
    renderApp("/");
    const banner = await screen.findByText(/Action needed:/);
    expect(banner.closest("[role=alert]")).toHaveTextContent("last backup failed · disk space low");
  });

  it("never asks a viewer's session for the owner-only operations status", async () => {
    renderApp("/", "viewer");
    await screen.findByRole("heading", { name: "Overview", level: 1 });
    expect(requests.some((r) => r.url.pathname === "/api/v1/admin/operations")).toBe(false);
  });

  it("surfaces a server-side refusal instead of failing silently", async () => {
    server.use(http.post("*/api/v1/providers/deepgram/sync", () =>
      HttpResponse.json({ error: { code: "forbidden", message: "Owner access required", request_id: "r" } }, { status: 403 })));
    const { user } = renderApp("/providers/deepgram");
    await user.click(await screen.findByRole("button", { name: /Sync now/ }));
    expect(await screen.findByText(/Owner access required/)).toBeInTheDocument();
  });

  it("shows a not-found page for unknown addresses", async () => {
    renderApp("/no/such/page");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to the overview" })).toHaveAttribute("href", "/");
  });
});

describe("AWS monitoring", () => {
  it("shows resource health and storage read-only, with no way to change resources", async () => {
    const seen = "2026-10-14T05:55:00Z";
    server.use(
      http.get("*/api/v1/providers/aws", () =>
        HttpResponse.json(provider({ id: "aws", display_name: "AWS", balance_capability: "postpaid", latest_balance: null, latest_balance_freshness: null, threshold: null, threshold_headroom: null }))),
      http.get("*/api/v1/aws/resources", () =>
        HttpResponse.json({
          observed_at: seen,
          is_stale: false,
          items: [
            { resource_type: "ec2_instance", resource_id: "i-0abc", region: "ap-south-1", name: "api-server", state: "running", health_status: "ok", details: { availability_zone: "ap-south-1a", instance_type: "t3.small", instance_status: "ok", system_status: "ok", scheduled_events: 0 }, observed_at: seen },
            { resource_type: "ec2_instance", resource_id: "i-0def", region: "ap-south-1", name: null, state: "running", health_status: "impaired", details: { instance_type: "t3.micro", instance_status: "impaired", system_status: "ok", scheduled_events: 1 }, observed_at: seen },
            { resource_type: "ebs_volume", resource_id: "vol-1", region: "ap-south-1", name: "data", state: "available", health_status: null, details: { size_gib: 100, volume_type: "gp3", attached_instances: [], encrypted: false }, observed_at: seen },
          ],
        }),
      ),
    );
    renderApp("/aws");
    const ec2 = await screen.findByRole("table", { name: "EC2 instances" });
    expect(within(ec2).getByText("api-server")).toBeInTheDocument();
    expect(within(ec2).getByText("Impaired")).toBeInTheDocument();
    const ebs = screen.getByRole("table", { name: "EBS volumes" });
    expect(within(ebs).getByText("Unattached")).toBeInTheDocument();
    expect(within(ebs).getByText("100 GiB")).toBeInTheDocument();
    expect(screen.getByText("Failing a status check").closest(".card")).toHaveTextContent("1");
    for (const name of [/stop/i, /start/i, /terminate/i, /delete/i, /resize/i, /reboot/i])
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    expect(requests.filter((r) => r.url.pathname.startsWith("/api/v1/aws") && r.method !== "GET")).toHaveLength(0);
  });
});
