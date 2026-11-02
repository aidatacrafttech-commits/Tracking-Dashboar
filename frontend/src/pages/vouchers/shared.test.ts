import { describe, expect, it, vi } from "vitest";
import { downloadCsv, getVoucherDisplayStatus, isExhausted, prettyKey } from "./shared";
import type { Voucher } from "./types";

type V = Parameters<typeof getVoucherDisplayStatus>[0];

const base: V = {
  deleted: false,
  status: "active",
  redeemable: true,
  limits_remaining: { total_remaining: 5, unique_users_remaining: 2 },
  total_limit: 10,
  max_unique_users: 3,
};
const used: Partial<V> = { redeemable: false, limits_remaining: { total_remaining: 0, unique_users_remaining: 0 } };
const show = (over: Partial<V>) => getVoucherDisplayStatus({ ...base, ...over });

describe("getVoucherDisplayStatus", () => {
  it("1. deleted wins over everything", () => {
    expect(show({ deleted: true, status: "active", ...used })).toMatchObject({ key: "deleted", label: "Deleted", tone: "unavailable" });
  });

  it("2-4. inactive, expired and scheduled win even when limits are used up", () => {
    expect(show({ status: "inactive", ...used })).toMatchObject({ label: "Inactive", tone: "unavailable" });
    expect(show({ status: "expired", ...used })).toMatchObject({ label: "Expired", tone: "critical" });
    expect(show({ status: "scheduled", ...used })).toMatchObject({ label: "Scheduled", tone: "info" });
  });

  it("5a. active but total limit used up -> Fully used with an explanation", () => {
    // AIGURU99: Used 2/2, unique users 2/3
    const d = show({ redeemable: false, total_limit: 2, limits_remaining: { total_remaining: 0, unique_users_remaining: 1 } });
    expect(d).toMatchObject({ key: "fully_used", label: "Fully used", tone: "warning" });
    expect(d.title).toBe("All 2 redemptions are used. Raise the total limit or create a new voucher.");
  });

  it("5b. active but unique users used up -> User limit reached", () => {
    const d = show({ redeemable: false, max_unique_users: 3, limits_remaining: { total_remaining: 4, unique_users_remaining: 0 } });
    expect(d).toMatchObject({ key: "user_limit_reached", label: "User limit reached", tone: "warning" });
    expect(d.title).toBe("All 3 unique users are used. Existing users may still redeem up to their per-user limit.");
  });

  it("5c. active, not redeemable, no exhausted limit -> Not redeemable", () => {
    const d = show({ redeemable: false, limits_remaining: { total_remaining: null, unique_users_remaining: null } });
    expect(d).toMatchObject({ key: "not_redeemable", label: "Not redeemable", tone: "warning" });
    expect(d.title).toBeTruthy();
  });

  it("6. active with capacity (or unlimited) -> Active", () => {
    expect(show({})).toMatchObject({ key: "active", label: "Active", tone: "normal" });
    expect(show({ total_limit: null, limits_remaining: { total_remaining: null, unique_users_remaining: null } })).toMatchObject({ key: "active" });
  });

  it("uses the singular for a limit of 1", () => {
    expect(show({ redeemable: false, total_limit: 1, limits_remaining: { total_remaining: 0, unique_users_remaining: null } }).title)
      .toBe("All 1 redemption is used. Raise the total limit or create a new voucher.");
  });

  it("isExhausted matches only Fully used and User limit reached", () => {
    expect(isExhausted({ ...base, ...used })).toBe(true);
    expect(isExhausted({ ...base, redeemable: false, limits_remaining: { total_remaining: 3, unique_users_remaining: 0 } })).toBe(true);
    expect(isExhausted(base)).toBe(false);
    expect(isExhausted({ ...base, ...used, status: "inactive" })).toBe(false);
    expect(isExhausted({ ...base, redeemable: false, limits_remaining: { total_remaining: null, unique_users_remaining: null } } as Voucher)).toBe(false);
  });
});

describe("redemption helpers", () => {
  it("prettifies subject and package keys", () => {
    expect(prettyKey("beginner")).toBe("Beginner");
    expect(prettyKey("class12_accountancy")).toBe("Class 12 Accountancy");
  });

  it("neutralises spreadsheet formulas in CSV but keeps phone numbers", async () => {
    let blob: Blob | undefined;
    URL.createObjectURL = vi.fn((b: Blob) => { blob = b; return "blob:x"; }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    downloadCsv("x.csv", ["a", "b", "c"], [["=HYPERLINK(1)", "+919800000001", 'say "hi"']]);
    const text = await blob!.text();
    expect(text).toContain(`"'=HYPERLINK(1)","+919800000001","say ""hi"""`);
  });
});
