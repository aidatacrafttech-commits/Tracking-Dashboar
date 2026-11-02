// Voucher Management against the AdhyaapakAI admin API (mocked with MSW; the real backend is
// never called from tests): admin-secret gate, 403 handling, Course and Live Tutor lists,
// create/edit/activate/deactivate/delete, 422 field errors, redemptions, subjects, packages.

import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/a11y";
import { renderApp } from "../test/render";
import { requests, server } from "../test/server";
import { adminSecret } from "./vouchers/adminApi";
import type { Voucher } from "./vouchers/types";

const SECRET = "test-admin-secret-value";
const A = "*/api/vouchers";

function voucher(over: Partial<Voucher> = {}): Voucher {
  return {
    id: "c1", category: "course", code: "SAVE20", title: "Diwali offer", description: null, discount_type: "percent",
    discount_value: 20, max_discount_inr: 100, min_purchase_inr: 0, starts_at: "2026-10-22T12:00:00Z", ends_at: "2026-10-31T18:29:00Z",
    created_at: "2026-10-20T10:00:00Z", updated_at: "2026-10-21T10:00:00Z", deleted_at: null, benefit_validity_days: 30,
    per_user_limit: 1, max_unique_users: null, total_limit: 100, enabled: true, all_subjects: false,
    subject_keys: ["class12_accountancy", "class12_economics"], status: "active", redeemable: true,
    stats: { total_redemptions: 3, completed_redemptions: 2, unique_users: 2 },
    limits_remaining: { total_remaining: 97, unique_users_remaining: null }, deleted: false, ...over,
  };
}
const COURSE = [
  voucher(),
  voucher({ id: "c2", code: "FLAT50", title: null, discount_type: "fixed", discount_value: 50, max_discount_inr: null, all_subjects: true,
    subject_keys: [], enabled: false, status: "inactive", stats: { total_redemptions: 0, completed_redemptions: 0, unique_users: 0 }, total_limit: null }),
];
const TUTOR = [
  voucher({ id: "t1", category: "live_tutor", code: "TUTOR50", discount_value: 50, max_discount_inr: null, all_subjects: undefined,
    subject_keys: undefined, all_packages: false, package_keys: ["beginner", "pro"] }),
];
const SUBJECTS = [
  { key: "class12_accountancy", name: "Class 12 Accountancy", class_level: 12, subject: "accountancy", product_ids: ["c12_acc"], builtin: true, active: true },
  { key: "class12_economics", name: "Class 12 Economics", class_level: 12, subject: "economics", product_ids: ["c12_eco"], builtin: true, active: true },
  { key: "spoken_english", name: "Spoken English", class_level: null, subject: null, product_ids: [], builtin: false, active: true },
];
const PACKAGES = [
  { key: "beginner", name: "Beginner", price_inr: 499, tokens: 100, active: true, sort_order: 1, token_pack_product_id: null, linked: false },
  { key: "intermediate", name: "Intermediate", price_inr: 999, tokens: 250, active: true, sort_order: 2, token_pack_product_id: "pack_250", linked: true },
  { key: "pro", name: "Pro", price_inr: 1999, tokens: 600, active: true, sort_order: 3, token_pack_product_id: null, linked: false },
];

const REDEMPTION = {
  id: "r1", voucher_code: "SAVE20", category: "course", user_id: "user_42", target_type: "course", target_id: "class12_accountancy",
  product_id: "c12_acc", original_amount_inr: 500, discount_inr: 100, final_amount_inr: 400, is_free: false, status: "completed",
  reserved_at: "2026-10-22T08:00:00Z", completed_at: "2026-10-22T08:05:00Z", order_id: "order_9", payment_id: "pay_9",
  benefit_expires_at: "2026-11-21T08:05:00Z", late: false,
  user: { id: "user_42", name: "Asha Verma", email: "asha@example.com", phone: "+919800000001" },
};

const authorized = (request: Request) => request.headers.get("x-admin-secret") === SECRET;
const forbidden = () => HttpResponse.json({ detail: "Forbidden." }, { status: 403 });
const secretHeaders: (string | null)[] = [];
const last = (method: string, path: string) => requests.filter((r) => r.method === method && r.url.pathname === path).at(-1)!;

function useAdhyaapak() {
  const guard = (handler: () => Response) => ({ request }: { request: Request }) => {
    secretHeaders.push(request.headers.get("x-admin-secret"));
    return authorized(request) ? handler() : forbidden();
  };
  server.use(
    http.get(`${A}/subjects`, guard(() => HttpResponse.json({ subjects: SUBJECTS }))),
    http.get(`${A}/live-tutor/packages/all`, guard(() => HttpResponse.json({ packages: PACKAGES }))),
    http.get(`${A}/courses`, guard(() => HttpResponse.json({ vouchers: COURSE, total: COURSE.length }))),
    http.get(`${A}/live-tutor`, guard(() => HttpResponse.json({ vouchers: TUTOR, total: TUTOR.length }))),
    http.get(`${A}/:prefix/:id/redemptions`, guard(() => HttpResponse.json({ total: 120, redemptions: [
      { ...REDEMPTION },
      { ...REDEMPTION, id: "r2", user_id: "user_gone", target_id: "beginner", is_free: true, final_amount_inr: 0, completed_at: null,
        reserved_at: "2026-10-21T04:30:00Z", status: "pending", user: { id: "user_gone", name: null, email: null, phone: null } },
    ] }))),
  );
}

beforeEach(() => {
  adminSecret.clear();
  secretHeaders.length = 0;
});

async function unlock(user: ReturnType<typeof renderApp>["user"]) {
  await user.type(await screen.findByLabelText("Admin secret"), SECRET);
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("admin secret", () => {
  it("asks for the secret, rejects a wrong one, and sends it as x-admin-secret", async () => {
    useAdhyaapak();
    const { user } = renderApp("/vouchers");
    expect(await screen.findByRole("heading", { name: "Unlock Voucher Management" })).toBeInTheDocument();
    expect(requests.some((r) => r.url.pathname === "/api/vouchers/courses")).toBe(false);
    await user.type(screen.getByLabelText("Admin secret"), "wrong-secret");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("That admin secret was not accepted.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Admin secret"));
    await unlock(user);
    expect(await screen.findByRole("table", { name: "Course vouchers" })).toBeInTheDocument();
    expect(secretHeaders.at(-1)).toBe(SECRET);
    expect(sessionStorage.getItem("adhyaapak-admin-secret")).toBe(SECRET);
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
  });

  it("returns to the secret screen when the backend answers 403", async () => {
    useAdhyaapak();
    adminSecret.set("stale-secret");
    renderApp("/vouchers");
    expect(await screen.findByText(/admin secret was not accepted/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Admin secret")).toBeInTheDocument();
    expect(sessionStorage.getItem("adhyaapak-admin-secret")).toBeNull();
  });

  it("signs out of the voucher admin and forgets the secret", async () => {
    useAdhyaapak();
    adminSecret.set(SECRET);
    const { user } = renderApp("/vouchers");
    await screen.findByRole("table", { name: "Course vouchers" });
    await user.click(screen.getByRole("button", { name: "Sign out of voucher admin" }));
    expect(await screen.findByLabelText("Admin secret")).toBeInTheDocument();
    expect(sessionStorage.getItem("adhyaapak-admin-secret")).toBeNull();
  });

  it("has no detectable accessibility violations", async () => {
    useAdhyaapak();
    const { container } = renderApp("/vouchers");
    await screen.findByLabelText("Admin secret");
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("course vouchers", () => {
  beforeEach(() => adminSecret.set(SECRET));

  it("lists vouchers with discount, scope, IST dates, status and usage", async () => {
    useAdhyaapak();
    const { container } = renderApp("/vouchers");
    const table = await screen.findByRole("table", { name: "Course vouchers" });
    const row = within(table).getByRole("row", { name: /SAVE20/ });
    expect(row).toHaveTextContent("20% off (max ₹100.00)");
    expect(await within(row).findByText("Class 12 Accountancy, Class 12 Economics")).toBeInTheDocument();
    expect(row).toHaveTextContent("22 Oct 2026, 05:30 pm IST");
    expect(row).toHaveTextContent("3 / 100");
    expect(row).toHaveTextContent("2 / ∞");
    expect(within(row).getByText("Active")).toBeInTheDocument();
    const flat = within(table).getByRole("row", { name: /FLAT50/ });
    expect(flat).toHaveTextContent("₹50.00 off");
    expect(flat).toHaveTextContent("All subjects");
    expect(within(flat).getByRole("button", { name: "Activate FLAT50" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows Fully used (amber, with a reason) for an active voucher whose limit is used up", async () => {
    useAdhyaapak();
    const aiguru = voucher({ id: "c7", code: "AIGURU99", total_limit: 2, max_unique_users: 3, redeemable: false,
      stats: { total_redemptions: 2, completed_redemptions: 2, unique_users: 2 },
      limits_remaining: { total_remaining: 0, unique_users_remaining: 1 } });
    let current = aiguru;
    server.use(
      http.get(`${A}/courses`, () => HttpResponse.json({ vouchers: [current, COURSE[0]], total: 2 })),
      http.patch(`${A}/courses/c7`, () => {
        current = { ...aiguru, total_limit: 5, redeemable: true, limits_remaining: { total_remaining: 3, unique_users_remaining: 1 } };
        return HttpResponse.json({ voucher: current });
      }),
    );
    const { user } = renderApp("/vouchers");
    const table = await screen.findByRole("table", { name: "Course vouchers" });
    const row = within(table).getByRole("row", { name: /AIGURU99/ });
    const badge = within(row).getByText("Fully used").closest(".badge")!;
    expect(badge).toHaveClass("tone-warning");
    expect(badge).toHaveAttribute("title", "All 2 redemptions are used. Raise the total limit or create a new voucher.");
    expect(row).toHaveTextContent("2 / 2");
    expect(within(within(table).getByRole("row", { name: /SAVE20/ })).getByText("Active").closest(".badge")).toHaveClass("tone-normal");
    // Still editable; raising the limit turns it back to Active from the new response.
    await user.click(within(row).getByRole("button", { name: "Edit AIGURU99" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Course voucher AIGURU99" });
    await user.clear(within(dialog).getByLabelText("Total redemption limit"));
    await user.type(within(dialog).getByLabelText("Total redemption limit"), "5");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await screen.findByText("Voucher AIGURU99 updated.");
    expect(last("PATCH", "/api/vouchers/courses/c7").body).toEqual({ total_limit: 5 });
    await waitFor(() => expect(within(screen.getByRole("row", { name: /AIGURU99/ })).getByText("Active")).toBeInTheDocument());
  });

  it("filters to fully used vouchers on the client", async () => {
    useAdhyaapak();
    const exhausted = voucher({ id: "c7", code: "AIGURU99", redeemable: false, limits_remaining: { total_remaining: 0, unique_users_remaining: 1 } });
    const userCap = voucher({ id: "c8", code: "FIRST3", redeemable: false, max_unique_users: 3, limits_remaining: { total_remaining: 10, unique_users_remaining: 0 } });
    server.use(http.get(`${A}/courses`, () => HttpResponse.json({ vouchers: [exhausted, userCap, COURSE[0]], total: 3 })));
    const { user } = renderApp("/vouchers");
    const table = await screen.findByRole("table", { name: "Course vouchers" });
    expect(within(table).getByText("User limit reached")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Status"), "fully_used");
    await waitFor(() => expect(within(table).queryByRole("row", { name: /SAVE20/ })).not.toBeInTheDocument());
    expect(within(table).getByRole("row", { name: /AIGURU99/ })).toBeInTheDocument();
    expect(within(table).getByRole("row", { name: /FIRST3/ })).toBeInTheDocument();
    expect(screen.getByText("2 fully used of the 3 active vouchers on this page.")).toBeInTheDocument();
    const sent = last("GET", "/api/vouchers/courses").url.searchParams;
    expect(sent.get("status")).toBe("active"); // "fully_used" is never sent to the API
    expect([...requests].some((r) => r.url.searchParams.get("status") === "fully_used")).toBe(false);
  });

  it("filters by status, search and deleted on the server", async () => {
    useAdhyaapak();
    const { user } = renderApp("/vouchers");
    await screen.findByRole("table", { name: "Course vouchers" });
    await user.selectOptions(screen.getByLabelText("Status"), "expired");
    await user.type(screen.getByLabelText("Search"), "diwali");
    await user.click(screen.getByRole("checkbox", { name: "Show deleted" }));
    await waitFor(() => {
      const p = last("GET", "/api/vouchers/courses").url.searchParams;
      expect(Object.fromEntries(p)).toMatchObject({ status: "expired", q: "diwali", include_deleted: "true", limit: "50", skip: "0" });
    });
  });

  it("validates, then creates with empty optional numbers omitted and +05:30 dates", async () => {
    useAdhyaapak();
    server.use(http.post(`${A}/courses`, async ({ request }) =>
      HttpResponse.json({ voucher: voucher({ id: "c9", code: ((await request.json()) as { code: string }).code }) }, { status: 201 })));
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "New Course voucher" }));
    const dialog = await screen.findByRole("dialog", { name: "Create Course voucher" });
    await user.type(within(dialog).getByLabelText("Code"), "ab");
    await user.click(within(dialog).getByRole("button", { name: "Create voucher" }));
    expect(await within(dialog).findByText(/3–32 characters/)).toBeInTheDocument();
    expect(within(dialog).getByText("Enter the discount.")).toBeInTheDocument();
    expect(within(dialog).getByText(/Choose at least one subject/)).toBeInTheDocument();
    expect(requests.some((r) => r.method === "POST")).toBe(false);

    await user.type(within(dialog).getByLabelText("Code"), "c50");
    await user.type(within(dialog).getByLabelText("Discount (%)"), "50");
    await user.click(await within(dialog).findByRole("checkbox", { name: "Class 12 Economics" }));
    await user.clear(within(dialog).getByLabelText("Starts at (IST)"));
    await user.type(within(dialog).getByLabelText("Starts at (IST)"), "2026-10-22T17:30");
    await user.type(within(dialog).getByLabelText("Ends at (IST)"), "2026-10-31T23:59");
    await user.click(within(dialog).getByRole("button", { name: "Create voucher" }));
    expect(await screen.findByText("Course voucher ABC50 created.")).toBeInTheDocument();
    expect(last("POST", "/api/vouchers/courses").body).toEqual({
      code: "ABC50", discount_type: "percent", discount_value: 50, starts_at: "2026-10-22T17:30:00+05:30",
      ends_at: "2026-10-31T23:59:00+05:30", per_user_limit: 1, enabled: true, all_subjects: false, subject_keys: ["class12_economics"],
    });
    expect(secretHeaders.at(-1)).toBe(SECRET);
  });

  it("shows 422 messages next to the field named in loc and keeps the values", async () => {
    useAdhyaapak();
    server.use(http.post(`${A}/courses`, () => HttpResponse.json({ detail: [
      { loc: ["body", "discount_value"], msg: "Input should be less than or equal to 100", type: "less_than_equal" },
      { loc: ["body", "subject_keys"], msg: "Unknown subject key", type: "value_error" },
    ] }, { status: 422 })));
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "New Course voucher" }));
    const dialog = await screen.findByRole("dialog", { name: "Create Course voucher" });
    await user.type(within(dialog).getByLabelText("Code"), "BADONE");
    await user.type(within(dialog).getByLabelText("Discount (%)"), "40");
    await user.click(within(dialog).getByRole("checkbox", { name: "All subjects" }));
    await user.click(within(dialog).getByRole("button", { name: "Create voucher" }));
    const value = within(dialog).getByLabelText("Discount (%)");
    await waitFor(() => expect(value).toHaveAccessibleDescription(/less than or equal to 100/));
    expect(within(dialog).getByText("Unknown subject key")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Code")).toHaveValue("BADONE");
  });

  it("shows business-rule (400) and conflict (409) details as-is", async () => {
    useAdhyaapak();
    server.use(http.post(`${A}/courses`, () => HttpResponse.json({ detail: "A voucher with code SAVE20 already exists." }, { status: 409 })));
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "New Course voucher" }));
    const dialog = await screen.findByRole("dialog", { name: "Create Course voucher" });
    await user.type(within(dialog).getByLabelText("Code"), "SAVE20");
    await user.type(within(dialog).getByLabelText("Discount (%)"), "20");
    await user.click(within(dialog).getByRole("checkbox", { name: "All subjects" }));
    await user.click(within(dialog).getByRole("button", { name: "Create voucher" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A voucher with code SAVE20 already exists.");
  });

  it("edits with PATCH of changed fields only, null to clear", async () => {
    useAdhyaapak();
    server.use(http.patch(`${A}/courses/c1`, () => HttpResponse.json({ voucher: voucher() })));
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "Edit SAVE20" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Course voucher SAVE20" });
    expect(within(dialog).getByLabelText("Code")).toBeDisabled(); // redeemed already
    expect(within(dialog).getByLabelText("Starts at (IST)")).toHaveValue("2026-10-22T17:30");
    expect(within(dialog).getByRole("checkbox", { name: "Class 12 Accountancy" })).toBeChecked();
    await user.clear(within(dialog).getByLabelText("Ends at (IST)"));
    await user.clear(within(dialog).getByLabelText("Total redemption limit"));
    await user.clear(within(dialog).getByLabelText("Title (optional)"));
    await user.type(within(dialog).getByLabelText("Title (optional)"), "Festive offer");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Voucher SAVE20 updated.")).toBeInTheDocument();
    expect(last("PATCH", "/api/vouchers/courses/c1").body).toEqual({ title: "Festive offer", ends_at: null, total_limit: null });
  });

  it("does not send a PATCH when nothing changed", async () => {
    useAdhyaapak();
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "Edit SAVE20" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Course voucher SAVE20" });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("No changes to save.")).toBeInTheDocument();
    expect(requests.some((r) => r.method === "PATCH")).toBe(false);
  });

  it("deactivates after confirming, activates directly, deletes after confirming", async () => {
    useAdhyaapak();
    server.use(
      http.post(`${A}/courses/c1/deactivate`, () => HttpResponse.json({ voucher: voucher({ enabled: false, status: "inactive" }) })),
      http.post(`${A}/courses/c2/activate`, () => HttpResponse.json({ voucher: voucher({ id: "c2", code: "FLAT50", status: "active" }) })),
      http.delete(`${A}/courses/c2`, () => HttpResponse.json({ deleted: true, id: "c2" })),
    );
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "Deactivate SAVE20" }));
    let dialog = await screen.findByRole("dialog", { name: "Deactivate voucher SAVE20?" });
    await user.click(within(dialog).getByRole("button", { name: "Deactivate" }));
    expect(await screen.findByText("SAVE20 is now inactive.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate FLAT50" }));
    expect(await screen.findByText("FLAT50 is now active.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete FLAT50" }));
    dialog = await screen.findByRole("dialog", { name: "Delete voucher FLAT50?" });
    expect(dialog).toHaveTextContent("history is kept");
    expect(dialog).toHaveTextContent("code becomes free");
    await user.click(within(dialog).getByRole("button", { name: "Delete voucher" }));
    expect(await screen.findByText("FLAT50 deleted.")).toBeInTheDocument();
    expect(requests.some((r) => r.method === "DELETE" && r.url.pathname === "/api/vouchers/courses/c2")).toBe(true);
  });

  it("shows a deleted voucher only with its original code and history", async () => {
    useAdhyaapak();
    server.use(http.get(`${A}/courses`, () => HttpResponse.json({ total: 1, vouchers: [
      voucher({ id: "c3", code: "SAVE20~deleted~c3", deleted: true, original_code: "SAVE20", deleted_at: "2026-10-22T05:00:00Z", status: "inactive" }),
    ] })));
    renderApp("/vouchers");
    const table = await screen.findByRole("table", { name: "Course vouchers" });
    expect(within(table).getByText("SAVE20")).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /SAVE20/ })).getAllByText("Deleted").length).toBeGreaterThan(0);
    expect(within(table).getByRole("button", { name: "Copy code SAVE20" })).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: /^Edit|^Delete/ })).not.toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Redemptions of SAVE20" })).toBeInTheDocument();
  });

  it("shows who used the voucher, with search, copy, CSV export and no PII in storage or logs", async () => {
    useAdhyaapak();
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((b: Blob) => { blobs.push(b); return "blob:csv"; }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    const log = vi.spyOn(console, "log");
    const { user } = renderApp("/vouchers");
    await user.click(await screen.findByRole("button", { name: "Redemptions of SAVE20" }));
    const drawer = await screen.findByRole("dialog", { name: "Redemptions — SAVE20" });
    const table = await within(drawer).findByRole("table", { name: "Redemptions of SAVE20" });
    expect(drawer).toHaveTextContent("120 redemptions · 2 users");
    const [asha, gone] = within(table).getAllByRole("row").slice(1) as [HTMLElement, HTMLElement];
    for (const text of ["Asha Verma", "user_42", "asha@example.com", "+919800000001", "₹500.00", "₹100.00", "₹400.00", "completed", "22 Oct 2026, 01:35 pm IST"])
      expect(asha).toHaveTextContent(text);
    expect(await within(asha).findByText("Class 12 Accountancy")).toBeInTheDocument();
    expect(gone).toHaveTextContent("Deleted account");
    expect(gone).toHaveTextContent("Free");
    expect(gone).toHaveTextContent("Beginner"); // prettified key
    expect(gone).toHaveTextContent("21 Oct 2026, 10:00 am IST"); // reserved_at when not completed
    expect(within(gone).getAllByText("—")).toHaveLength(2); // no email, no phone

    await user.click(within(asha).getByRole("button", { name: "Copy email asha@example.com" }));
    expect(await navigator.clipboard.readText()).toBe("asha@example.com"); // user-event's clipboard stub
    expect(await screen.findByText("Email copied.")).toBeInTheDocument();

    await user.type(within(drawer).getByLabelText("Search this page"), "asha@");
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2));
    await user.click(within(drawer).getByRole("button", { name: "Export CSV" }));
    const csv = await blobs[0]!.text();
    expect(csv).toContain('"Code","Name","Email","Phone","Package/Course","Paid (INR)","Status","Date (IST)"');
    expect(csv).toContain('"SAVE20","Asha Verma","asha@example.com","+919800000001","Class 12 Accountancy","400","completed"');
    expect(csv).not.toContain("Deleted account"); // only the filtered rows

    await user.click(within(drawer).getByRole("button", { name: "Next" }));
    await waitFor(() => expect(last("GET", "/api/vouchers/courses/c1/redemptions").url.searchParams.get("skip")).toBe("50"));

    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(stored).not.toContain("asha@example.com");
    expect(stored).not.toContain("+919800000001");
    expect(JSON.stringify(log.mock.calls)).not.toContain("asha@example.com");
  });
});

describe("live tutor vouchers", () => {
  beforeEach(() => adminSecret.set(SECRET));

  it("lists packages and creates a voucher for several packages", async () => {
    useAdhyaapak();
    server.use(http.post(`${A}/live-tutor`, () => HttpResponse.json({ voucher: TUTOR[0] }, { status: 201 })));
    const { user } = renderApp("/vouchers?tab=live-tutor");
    const table = await screen.findByRole("table", { name: "Live Tutor vouchers" });
    expect(await within(table).findByText("Beginner, Pro")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New Live Tutor voucher" }));
    const dialog = await screen.findByRole("dialog", { name: "Create Live Tutor voucher" });
    await user.type(within(dialog).getByLabelText("Code"), "TUTOR50");
    await user.selectOptions(within(dialog).getByLabelText("Discount type"), "fixed");
    await user.type(within(dialog).getByLabelText("Discount (₹)"), "200");
    await user.type(within(dialog).getByLabelText("Minimum purchase (₹)"), "500");
    await user.click(within(dialog).getByRole("checkbox", { name: /^Pro/ }));
    await user.click(within(dialog).getByRole("checkbox", { name: /^Intermediate/ }));
    await user.click(within(dialog).getByRole("button", { name: "Create voucher" }));
    await screen.findByText("Live Tutor voucher TUTOR50 created.");
    expect(last("POST", "/api/vouchers/live-tutor").body).toMatchObject({
      code: "TUTOR50", discount_type: "fixed", discount_value: 200, min_purchase_inr: 500, all_packages: false,
      package_keys: ["intermediate", "pro"],
    });
    expect(last("POST", "/api/vouchers/live-tutor").body).not.toHaveProperty("max_discount_inr");
  });

  it("restores the package selection when editing", async () => {
    useAdhyaapak();
    const { user } = renderApp("/vouchers?tab=live-tutor");
    await user.click(await screen.findByRole("button", { name: "Edit TUTOR50" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Live Tutor voucher TUTOR50" });
    expect(within(dialog).getByRole("checkbox", { name: /^Beginner/ })).toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: /^Intermediate/ })).not.toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: /^Pro/ })).toBeChecked();
  });
});

describe("redemptions tab", () => {
  beforeEach(() => adminSecret.set(SECRET));

  it("shows which code each user used and for what, across both categories", async () => {
    useAdhyaapak();
    const row = (id: string, code: string, category: string, user: string, target: string, at: string) => ({
      id, voucher_code: code, category, user_id: user, target_type: category === "course" ? "course" : "live_tutor", target_id: target,
      product_id: null, original_amount_inr: 999, discount_inr: 100, final_amount_inr: 899, is_free: false, status: "completed",
      reserved_at: at, completed_at: at, order_id: `order_${id}`, payment_id: null, benefit_expires_at: null, late: false,
    });
    server.use(http.get(`${A}/:prefix/:id/redemptions`, ({ params }) => HttpResponse.json(params.id === "c1"
      ? { total: 1, redemptions: [row("r1", "SAVE20", "course", "user_42", "class12_accountancy", "2026-10-22T08:00:00Z")] }
      : { total: 1, redemptions: [row("r2", "TUTOR50", "live_tutor", "user_77", "pro", "2026-10-22T09:00:00Z")] })));
    const { user } = renderApp("/vouchers?tab=redemptions");
    const table = await screen.findByRole("table", { name: "Voucher redemptions" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("TUTOR50"); // newest first
    expect(rows[0]).toHaveTextContent("user_77");
    expect(rows[0]).toHaveTextContent("Pro");
    expect(rows[1]).toHaveTextContent("SAVE20");
    expect(rows[1]).toHaveTextContent("user_42");
    expect(await within(rows[1]!).findByText("Class 12 Accountancy")).toBeInTheDocument();
    // Only vouchers that were used are queried (FLAT50 has no redemptions).
    expect(requests.some((r) => r.url.pathname === "/api/vouchers/courses/c2/redemptions")).toBe(false);
    await user.type(screen.getByLabelText("Search"), "user_42");
    await waitFor(() => expect(within(table).queryByText("TUTOR50")).not.toBeInTheDocument());
    expect(within(table).getByText("SAVE20")).toBeInTheDocument();
  });
});

describe("subjects and packages", () => {
  beforeEach(() => adminSecret.set(SECRET));

  it("adds a custom subject, deactivates one and reports a 409 on delete", async () => {
    useAdhyaapak();
    server.use(
      http.post(`${A}/subjects`, () => HttpResponse.json({ subject: SUBJECTS[2] }, { status: 201 })),
      http.patch(`${A}/subjects/:key`, () => HttpResponse.json({ subject: SUBJECTS[0] })),
      http.delete(`${A}/subjects/:key`, () => HttpResponse.json({ detail: "Subject is used by voucher SAVE20." }, { status: 409 })),
    );
    const { user } = renderApp("/vouchers?tab=subjects");
    const table = await screen.findByRole("table", { name: "Voucher subjects" });
    expect(within(table).queryByRole("button", { name: "Delete Class 12 Accountancy" })).not.toBeInTheDocument(); // built-in
    await user.click(screen.getByRole("button", { name: "Add custom subject" }));
    let dialog = await screen.findByRole("dialog", { name: "Add custom subject" });
    await user.type(within(dialog).getByLabelText("Name"), "Spoken English");
    await user.type(within(dialog).getByLabelText("Product IDs (optional)"), "eng_basic, eng_pro");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await screen.findByText("Subject Spoken English added.");
    expect(last("POST", "/api/vouchers/subjects").body).toEqual({ name: "Spoken English", product_ids: ["eng_basic", "eng_pro"], active: true });

    await user.click(screen.getByRole("button", { name: "Deactivate Class 12 Economics" }));
    await waitFor(() => expect(last("PATCH", "/api/vouchers/subjects/class12_economics").body).toEqual({ active: false }));

    await user.click(screen.getByRole("button", { name: "Edit Class 12 Accountancy" }));
    dialog = await screen.findByRole("dialog", { name: "Edit subject Class 12 Accountancy" });
    expect(within(dialog).queryByLabelText("Class (optional)")).not.toBeInTheDocument(); // built-in: name/active only
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Delete Spoken English" }));
    dialog = await screen.findByRole("dialog", { name: "Delete subject Spoken English?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete subject" }));
    expect(await screen.findByText(/Subject is used by voucher SAVE20\./)).toBeInTheDocument();
  });

  it("edits a package with PUT", async () => {
    useAdhyaapak();
    server.use(http.put(`${A}/live-tutor/packages/:key`, () => HttpResponse.json({ package: PACKAGES[0] })));
    const { user } = renderApp("/vouchers?tab=packages");
    const table = await screen.findByRole("table", { name: "Live Tutor packages" });
    expect(within(table).getByText("Linked")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit Beginner" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit package Beginner" });
    await user.clear(within(dialog).getByLabelText("Price (₹)"));
    await user.type(within(dialog).getByLabelText("Price (₹)"), "599");
    await user.type(within(dialog).getByLabelText("Token pack product ID (optional)"), "pack_100");
    await user.click(within(dialog).getByRole("button", { name: "Save package" }));
    await screen.findByText("Beginner saved.");
    expect(last("PUT", "/api/vouchers/live-tutor/packages/beginner").body).toEqual({
      name: "Beginner", price_inr: 599, tokens: 100, active: true, sort_order: 1, token_pack_product_id: "pack_100",
    });
  });
});
