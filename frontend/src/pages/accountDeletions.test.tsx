// Account Deletions page against the dashboard's own API (mocked with MSW). The browser
// never talks to AdhyaapakAI and never holds its admin secret.

import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { axeViolations } from "../test/a11y";
import { renderApp } from "../test/render";
import { CSRF, requests, server } from "../test/server";

const D = "*/api/v1/account-deletions";
const LIST = {
  count: 2,
  grace_days: 7,
  deletions: [
    { user_id: "u1", email: "amit@example.com", name: "Amit", requested_at: "2026-11-01T05:00:00Z", due_at: "2026-11-08T05:00:00Z", days_left: 1 },
    { user_id: "u2", email: "neha@example.com", name: null, requested_at: "2026-11-02T05:00:00Z", due_at: "2026-11-09T05:00:00Z", days_left: 5 },
  ],
};
const posts = (path: string) => requests.filter((r) => r.method === "POST" && r.url.pathname === `/api/v1/account-deletions/${path}`);

function useApi(list: object = LIST) {
  server.use(
    http.get(`${D}/status`, () => HttpResponse.json({ configured: true, target: "api.adhyaapak.ai" })),
    http.get(D, () => HttpResponse.json(list)),
  );
}

describe("account deletions", () => {
  it("lists pending deletions with IST dates, highlights the last day and searches by email", async () => {
    useApi();
    const { user } = renderApp("/account-deletions");
    expect(await screen.findByText(/2 accounts waiting for deletion · 7-day grace period/)).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Accounts waiting for deletion" });
    const amit = within(table).getByRole("row", { name: /amit@example.com/ });
    expect(amit).toHaveTextContent("1 Nov 2026, 10:30 am IST");
    expect(amit).toHaveTextContent("8 Nov 2026, 10:30 am IST");
    expect(within(amit).getByText("1 day").closest(".badge")).toHaveClass("tone-critical");
    expect(within(table).getByText("5 days").closest(".badge")).not.toHaveClass("tone-critical");
    await user.type(screen.getByLabelText("Search by email"), "neha");
    expect(within(table).queryByText("amit@example.com")).not.toBeInTheDocument();
    expect(within(table).getByText("neha@example.com")).toBeInTheDocument();
    expect(JSON.stringify(requests.map((r) => r.url.pathname))).not.toContain("/api/admin/");
  });

  it("shows a clear Not authorised state when the server's secret is rejected", async () => {
    useApi();
    server.use(http.get(D, () => HttpResponse.json({ error: { code: "adhyaapak_forbidden", message: "Not authorised: rejected", request_id: "r" } }, { status: 502 })));
    renderApp("/account-deletions");
    expect(await screen.findByText("Not authorised.")).toBeInTheDocument();
    expect(screen.getByText("ADHYAAPAK_ADMIN_SECRET")).toBeInTheDocument();
  });

  it("shows a setup notice and never requests the list when the server is not configured", async () => {
    server.use(http.get(`${D}/status`, () => HttpResponse.json({ configured: false, target: null })));
    renderApp("/account-deletions");
    expect(await screen.findByText("Account deletions are not configured.")).toBeInTheDocument();
    expect(requests.some((r) => r.url.pathname === "/api/v1/account-deletions")).toBe(false);
  });

  it("shows the empty state", async () => {
    useApi({ count: 0, grace_days: 7, deletions: [] });
    renderApp("/account-deletions");
    expect(await screen.findByText("No accounts are waiting for deletion.")).toBeInTheDocument();
  });

  it("restores only after verification and a note, then removes the row", async () => {
    useApi();
    server.use(http.post(`${D}/reactivate`, () => HttpResponse.json({ success: true, status: "reactivated", user_id: "u1", email: "amit@example.com", already: false })));
    const { user } = renderApp("/account-deletions");
    await user.click(await screen.findByRole("button", { name: "Restore account amit@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore this account?" });
    expect(dialog).toHaveTextContent("amit@example.com");
    expect(dialog).toHaveTextContent("8 Nov 2026, 10:30 am IST");
    const submit = within(dialog).getByRole("button", { name: "Restore account" });
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Note (ticket id, who asked)"), "ticket 123");
    expect(submit).toBeDisabled(); // checkbox still missing
    await user.click(within(dialog).getByRole("checkbox", { name: /verified this request came from the registered email/ }));
    expect(submit).toBeEnabled();
    await user.click(submit);
    expect(await screen.findByText("Account restored — the student can sign in again")).toBeInTheDocument();
    expect(posts("reactivate")).toHaveLength(1);
    expect(posts("reactivate")[0]!.body).toEqual({ email: "amit@example.com", note: "ticket 123" });
    expect(posts("reactivate")[0]!.csrf).toBe(CSRF);
    expect(screen.queryByRole("row", { name: /amit@example.com/ })).not.toBeInTheDocument();
  });

  it("treats a repeat restore (already: true) as success", async () => {
    useApi();
    server.use(http.post(`${D}/reactivate`, () => HttpResponse.json({ success: true, status: "reactivated", user_id: "u1", email: "amit@example.com", already: true })));
    const { user } = renderApp("/account-deletions");
    await user.click(await screen.findByRole("button", { name: "Restore account amit@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore this account?" });
    await user.click(within(dialog).getByRole("checkbox"));
    await user.type(within(dialog).getByLabelText("Note (ticket id, who asked)"), "ticket 9");
    await user.click(within(dialog).getByRole("button", { name: "Restore account" }));
    expect(await screen.findByText(/already restored/)).toBeInTheDocument();
  });

  it("shows the backend detail and refreshes when a restore is refused (409/410)", async () => {
    useApi();
    server.use(http.post(`${D}/reactivate`, () =>
      HttpResponse.json({ error: { code: "adhyaapak_410", message: "Permanently deleted on 2026-10-30.", request_id: "r" } }, { status: 410 })));
    const { user } = renderApp("/account-deletions");
    await user.click(await screen.findByRole("button", { name: "Restore account amit@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore this account?" });
    await user.click(within(dialog).getByRole("checkbox"));
    await user.type(within(dialog).getByLabelText("Note (ticket id, who asked)"), "ticket 4");
    const lists = requests.filter((r) => r.method === "GET" && r.url.pathname === "/api/v1/account-deletions").length;
    await user.click(within(dialog).getByRole("button", { name: "Restore account" }));
    expect(await screen.findByText(/Permanently deleted on 2026-10-30\./)).toBeInTheDocument();
    await waitFor(() => expect(requests.filter((r) => r.method === "GET" && r.url.pathname === "/api/v1/account-deletions").length).toBeGreaterThan(lists));
  });

  it("purges only after the email is typed and a note given; a 409 is shown, never retried", async () => {
    useApi();
    server.use(http.post(`${D}/purge-now`, () =>
      HttpResponse.json({ error: { code: "adhyaapak_409", message: "No pending deletion request for this account.", request_id: "r" } }, { status: 409 })));
    const { user } = renderApp("/account-deletions");
    await user.click(await screen.findByRole("button", { name: "Purge now neha@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Permanently delete this account now?" });
    const submit = within(dialog).getByRole("button", { name: "Purge now" });
    await user.type(within(dialog).getByLabelText("Type the email address to confirm"), "neha@example.co");
    await user.type(within(dialog).getByLabelText("Note (why it is deleted now)"), "asked for immediate deletion");
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type the email address to confirm"), "m");
    expect(submit).toBeEnabled();
    await user.click(submit);
    expect(await within(dialog).findByText("No pending deletion request for this account.")).toBeInTheDocument();
    expect(posts("purge-now")).toHaveLength(1);
    expect(posts("purge-now")[0]!.body).toEqual({ email: "neha@example.com", note: "asked for immediate deletion" });
    expect(screen.getByRole("row", { name: /neha@example.com/ })).toBeInTheDocument();
  });

  it("removes the row after a successful purge", async () => {
    useApi();
    server.use(http.post(`${D}/purge-now`, () => HttpResponse.json({ success: true, status: "completed", user_id: "u2" })));
    const { user } = renderApp("/account-deletions");
    await user.click(await screen.findByRole("button", { name: "Purge now neha@example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Permanently delete this account now?" });
    await user.type(within(dialog).getByLabelText("Type the email address to confirm"), "NEHA@example.com");
    await user.type(within(dialog).getByLabelText("Note (why it is deleted now)"), "asked");
    await user.click(within(dialog).getByRole("button", { name: "Purge now" }));
    expect(await screen.findByText("Account permanently deleted.")).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /neha@example.com/ })).not.toBeInTheDocument();
  });

  it("gives viewers the list without actions", async () => {
    useApi();
    renderApp("/account-deletions", "viewer");
    await screen.findByRole("table", { name: "Accounts waiting for deletion" });
    expect(screen.queryByRole("button", { name: /Restore account|Purge now/ })).not.toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    useApi();
    const { container } = renderApp("/account-deletions");
    await screen.findByRole("table", { name: "Accounts waiting for deletion" });
    expect(await axeViolations(container)).toEqual([]);
  });
});
