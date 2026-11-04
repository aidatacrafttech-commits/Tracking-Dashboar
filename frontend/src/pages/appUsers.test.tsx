// App Users page against the dashboard's own API (mocked with MSW). The browser never talks
// to the app database and only ever receives counts and server-masked rows.

import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { axeViolations } from "../test/a11y";
import { renderApp } from "../test/render";
import { requests, server } from "../test/server";

const A = "*/api/v1/app-users";
const day = (date: string, signups: number, deleted_since = 0) => ({ date, signups, deleted_since, purchases: 0, vouchers: 0, revenue_paise: 0 });
const SUMMARY = {
  timezone: "Asia/Kolkata",
  start: "2026-10-28",
  end: "2026-11-03",
  days: 7,
  generated_at: "2026-11-03T09:30:00Z",
  active_tracked_since: "2026-10-28",
  today: {
    date: "2026-11-03", signups: 12, signups_yesterday: 9, active_today: 340, active_7d: 1200, active_30d: 2600,
    purchases: 2, vouchers: 1, revenue_paise: 399_800, deletions_scheduled: 3,
  },
  totals: {
    signups: 61, deleted_since: 1, average_per_day: 8.7, best_day: "2026-11-01", best_day_signups: 15, with_course: 6,
    purchases: 9, vouchers: 2, revenue_paise: 1_799_100, other_currency_purchases: 0, total_users: 15234,
  },
  daily: [
    day("2026-10-28", 5), day("2026-10-29", 8), day("2026-10-30", 7, 1), day("2026-10-31", 4),
    day("2026-11-01", 15), day("2026-11-02", 9), day("2026-11-03", 12),
  ],
  by_class: [{ label: "Class 12", count: 40 }, { label: "Class 11", count: 20 }],
  by_stream: [{ label: "Commerce", count: 50 }, { label: "Not set", count: 10 }],
  by_goal: [{ label: "Board", count: 55 }, { label: "JEE", count: 4 }],
};
const TODAY = {
  start: "2026-11-03",
  end: "2026-11-03",
  timezone: "Asia/Kolkata",
  masked: true,
  more: false,
  signups: [
    {
      signed_up_at: "2026-11-03T08:35:00Z", name: "Asha", class_level: 12, stream: "Commerce", goals: ["Board"],
      email: "a***@gmail.com", phone: "******3210", has_course: true, deletion_scheduled: false,
    },
  ],
};
const LIVE_NOW = { online_now: 4, app_open: null, using_app: 3, watching_video: 2, in_live_tutor: 1, asking_doubts: 0, taking_test: 0, online_window_minutes: 12, checked_at: "2026-11-03T09:30:00Z" };
const called = (path: string) => requests.some((r) => r.url.pathname.startsWith(`/api/v1/app-users/${path}`));

function useApi(summary: object = SUMMARY, today: object = TODAY) {
  server.use(
    http.get(`${A}/status`, () => HttpResponse.json({ configured: true, database: "doubt_solver_app" })),
    http.get(`${A}/summary`, () => HttpResponse.json(summary)),
    http.get(`${A}/signups`, () => HttpResponse.json(today)),
    http.get(`${A}/live`, () => HttpResponse.json(LIVE_NOW)),
  );
}

describe("app users", () => {
  it("shows today's numbers, the masked signups list and the daily chart table", async () => {
    useApi();
    renderApp("/app-users");
    // First test in the file: the lazily loaded page can take over a second under a full run.
    const signups = await screen.findByRole("region", { name: "New signups today" }, { timeout: 5000 });
    expect(signups).toHaveTextContent("12");
    expect(signups).toHaveTextContent("Yesterday 9 · 3 more today");
    const online = screen.getByRole("region", { name: "Online now" });
    expect(await within(online).findByText("4")).toBeInTheDocument();
    expect(online).toHaveTextContent("2 watching a video · 1 in Live Tutor · 340 active today");
    expect(screen.getByRole("region", { name: "Course purchases today" })).toHaveTextContent("1 by voucher");
    expect(screen.getByRole("region", { name: "Total users" })).toHaveTextContent("15,234");

    const table = await screen.findByRole("table", { name: "Today's signups" });
    const row = within(table).getByRole("row", { name: /Asha/ });
    expect(row).toHaveTextContent("a***@gmail.com");
    expect(row).toHaveTextContent("******3210");
    expect(row).toHaveTextContent("2:05 pm IST");
    expect(within(row).getByText("Has course")).toBeInTheDocument();

    const data = screen.getByRole("table", { name: /New signups per day, 7 days data/ });
    expect(within(data).getByRole("row", { name: /2026-11-01/ })).toHaveTextContent("15");
    expect(screen.getByRole("table", { name: "Signups by class" })).toHaveTextContent("Class 12");
    expect(screen.getByRole("region", { name: "Best day" })).toHaveTextContent("1 Nov 2026");
  });

  it("updates Online now as people start and stop using the app", async () => {
    useApi();
    let live = { ...LIVE_NOW, online_now: 5, asking_doubts: 2 };
    server.use(http.get(`${A}/live`, () => HttpResponse.json(live)));
    const { queryClient } = renderApp("/app-users");
    const online = await screen.findByRole("region", { name: "Online now" });
    expect(await within(online).findByText("5")).toBeInTheDocument();
    expect(online).toHaveTextContent("2 asking doubts");
    live = { ...LIVE_NOW, online_now: 1 };
    await queryClient.invalidateQueries({ queryKey: ["app-users", "live"] });
    expect(await within(online).findByText("1")).toBeInTheDocument();
    expect(online).not.toHaveTextContent("asking doubts");
  });

  it("asks for the selected range", async () => {
    useApi();
    const { user } = renderApp("/app-users");
    await screen.findByRole("region", { name: "New signups today" });
    await user.selectOptions(screen.getByLabelText("Range"), "90");
    await waitFor(() =>
      expect(requests.some((r) => r.url.pathname === "/api/v1/app-users/summary" && r.url.searchParams.get("days") === "90")).toBe(true));
  });

  it("asks for a custom date range only once both dates are valid", async () => {
    useApi();
    const { user } = renderApp("/app-users");
    await screen.findByRole("region", { name: "New signups today" });
    await user.selectOptions(screen.getByLabelText("Range"), "custom");
    expect(screen.getByText(/Choose a start and end date/)).toBeInTheDocument();
    const to = screen.getByLabelText("To");
    await user.clear(to);
    await user.type(to, "2026-10-20");
    await user.type(screen.getByLabelText("From"), "2026-10-25");
    expect(await screen.findByText("The start date is after the end date.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("From"));
    await user.type(screen.getByLabelText("From"), "2026-10-10");
    await waitFor(() =>
      expect(requests.some((r) => r.url.pathname === "/api/v1/app-users/summary"
        && r.url.searchParams.get("start") === "2026-10-10" && r.url.searchParams.get("end") === "2026-10-20")).toBe(true));
  });

  it("lists another day's signups from the date picker or by clicking a day, with full details for the owner", async () => {
    useApi();
    server.use(http.get(`${A}/signups`, ({ request }) => {
      const params = new URL(request.url).searchParams;
      const [start, end] = [params.get("start"), params.get("end")];
      const ravi = start !== null && start <= "2026-11-01" && end !== null && end >= "2026-11-01";
      return HttpResponse.json({
        ...TODAY, start, end, masked: false,
        signups: ravi
          ? [{ ...TODAY.signups[0], name: "Ravi Kumar", email: "ravi.kumar@gmail.com", phone: "9123456780", has_course: false }]
          : [],
      });
    }));
    const { user } = renderApp("/app-users");
    await screen.findByRole("region", { name: "New signups today" });
    await user.click(screen.getByRole("button", { name: "Show signups on 2026-11-01" }));
    const table = await screen.findByRole("table", { name: "Signups on 1 Nov 2026" });
    const row = within(table).getByRole("row", { name: /Ravi Kumar/ });
    expect(row).toHaveTextContent("ravi.kumar@gmail.com");
    expect(row).toHaveTextContent("9123456780");
    await user.type(screen.getByLabelText("Search name, email or phone"), "nobody");
    expect(await screen.findByText("No one matches this search.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search name, email or phone"));
    const from = screen.getByLabelText("From date");
    await user.clear(from);
    await user.type(from, "2026-10-28");
    const range = await screen.findByRole("table", { name: "Signups from 28 Oct 2026 to 1 Nov 2026" });
    expect(within(range).getByRole("row", { name: /Ravi Kumar/ })).toBeInTheDocument();
    expect(requests.some((r) => r.url.searchParams.get("start") === "2026-10-28" && r.url.searchParams.get("end") === "2026-11-01")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Today" }));
    expect(await screen.findByText("No one has signed up yet today.")).toBeInTheDocument();
  });

  it("switches the list to deletion requests, with the profile only while it still exists", async () => {
    useApi();
    server.use(http.get(`${A}/deletions`, ({ request }) => {
      const params = new URL(request.url).searchParams;
      return HttpResponse.json({
        start: params.get("start"), end: params.get("end"), timezone: "Asia/Kolkata", masked: false, more: false,
        deletions: [
          { requested_at: "2026-11-03T04:30:00Z", status: "scheduled", due_at: "2026-11-10T04:30:00Z", completed_at: null, reactivated_at: null,
            signed_up_at: "2026-10-01T04:30:00Z", profile_exists: true, name: "Neha Singh", email: "neha.singh@gmail.com", phone: "9988776655" },
          { requested_at: "2026-11-03T02:30:00Z", status: "completed", due_at: null, completed_at: "2026-11-03T02:31:00Z", reactivated_at: null,
            signed_up_at: "2026-09-01T04:30:00Z", profile_exists: false, name: null, email: null, phone: null },
        ],
      });
    }));
    const { user } = renderApp("/app-users");
    await screen.findByRole("table", { name: "Today's signups" });
    const show = screen.getByRole("combobox", { name: "Show" });
    expect(show).toHaveValue("signups");
    await user.selectOptions(show, "deletions");
    const table = await screen.findByRole("table", { name: "Today's deletion requests" });
    const neha = within(table).getByRole("row", { name: /Neha Singh/ });
    expect(neha).toHaveTextContent("neha.singh@gmail.com");
    expect(neha).toHaveTextContent("9988776655");
    expect(within(neha).getByText("Scheduled")).toBeInTheDocument();
    expect(neha).toHaveTextContent("Due 10 Nov 2026");
    const gone = within(table).getByRole("row", { name: /Details removed/ });
    expect(within(gone).getByText("Deleted")).toBeInTheDocument();
    expect(screen.getByTestId("people-count")).toHaveTextContent("2 deletion requests");
    await user.type(screen.getByLabelText("Search name, email or phone"), "neha");
    expect(within(table).queryByText("Details removed")).not.toBeInTheDocument();
    expect(screen.getByTestId("people-count")).toHaveTextContent("1 of 2 shown");
    expect(requests.some((r) => r.url.pathname === "/api/v1/app-users/deletions")).toBe(true);
  });

  it("shows a setup notice and never requests data when the server is not configured", async () => {
    server.use(http.get(`${A}/status`, () => HttpResponse.json({ configured: false, database: null })));
    renderApp("/app-users");
    expect(await screen.findByText("App users are not configured.")).toBeInTheDocument();
    expect(called("summary") || called("signups")).toBe(false);
  });

  it("shows Not authorised when the database refuses the dashboard's login", async () => {
    useApi();
    server.use(http.get(`${A}/summary`, () =>
      HttpResponse.json({ error: { code: "app_users_forbidden", message: "refused", request_id: "r" } }, { status: 502 })));
    renderApp("/app-users");
    expect(await screen.findByText("Not authorised.")).toBeInTheDocument();
    expect(screen.getByText("ADHYAAPAK_MONGO_URL")).toBeInTheDocument();
  });

  it("shows an empty state when no one has signed up today", async () => {
    useApi(SUMMARY, { ...TODAY, signups: [] });
    renderApp("/app-users");
    expect(await screen.findByText("No one has signed up yet today.")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    useApi();
    const { container } = renderApp("/app-users");
    await screen.findByRole("table", { name: "Today's signups" });
    expect(await axeViolations(container)).toEqual([]);
  });
});
