import { describe, expect, it } from "vitest";
import { formatAge, formatAmount, formatDateTime, formatMoney, formatSigned, isCurrency } from "./format";

describe("money formatting", () => {
  it("formats each record in its own currency", () => {
    expect(formatMoney("1250.75", "USD")).toContain("1,250.75");
    expect(formatMoney("1250.75", "USD")).toMatch(/\$/);
    expect(formatMoney("2000", "INR")).toMatch(/₹|INR/);
    expect(formatMoney("2000", "INR")).toContain("2,000.00");
  });

  it("formats from the decimal string without float drift", () => {
    // 0.1 + 0.2 style values must not become 0.30000000000000004
    expect(formatMoney("0.3", "USD")).toContain("0.30");
    // Beyond float precision: Intl formats the exact string. Grouping follows the user's
    // locale (en-IN gives 1,23,45,67,89,012.34), so compare the digits only.
    expect(formatMoney("123456789012.34", "USD").replace(/[^\d.]/g, "")).toBe("123456789012.34");
    expect(formatMoney("9007199254740993.01", "USD").replace(/[^\d.]/g, "")).toBe("9007199254740993.01");
  });

  it("shows unknown instead of zero for missing values", () => {
    expect(formatMoney(null, "USD")).toBe("Unknown");
    expect(formatAmount(undefined, "CREDITS")).toBe("Unknown");
  });

  it("formats credits and quantities as non-money", () => {
    expect(formatAmount("9000", "CREDITS")).toBe("9,000 credits");
    expect(formatAmount("1619.724207", "hours")).toBe("1,619.72 hours");
    expect(isCurrency("CREDITS")).toBe(false);
    expect(isCurrency("USD")).toBe(true);
  });

  it("signs differences", () => {
    expect(formatSigned("-6.6", "USD")).toMatch(/^−\$6\.60$/);
    expect(formatSigned("12.4", "USD")).toMatch(/^\+\$12\.40$/);
    expect(formatSigned("0", "USD")).toBe("$0.00");
  });
});

describe("time formatting", () => {
  it("uses the configured display timezone", () => {
    expect(formatDateTime("2026-10-13T06:26:00Z", "Asia/Kolkata")).toMatch(/11:56/);
    expect(formatDateTime("2026-10-13T06:26:00Z", "UTC")).toMatch(/06:26/);
    expect(formatDateTime(null, "UTC")).toBe("Never");
  });

  it("describes ages", () => {
    const now = Date.parse("2026-10-14T06:00:00Z");
    expect(formatAge("2026-10-14T05:56:00Z", now)).toBe("4 min ago");
    expect(formatAge("2026-10-14T03:00:00Z", now)).toBe("3 h ago");
    expect(formatAge("2026-10-10T06:00:00Z", now)).toBe("4 days ago");
    expect(formatAge(null, now)).toBe("never");
  });
});
