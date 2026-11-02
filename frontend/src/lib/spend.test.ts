import { describe, expect, it } from "vitest";
import type { OverviewProvider } from "../api/types";
import { overview } from "../test/fixtures";
import { spendThisMonth } from "./spend";

const base: OverviewProvider = { ...overview.providers[0]!, month_to_date_cost: [], month_to_date_usage: [], failing_operations: [] };
const total = (metric: string, unit: string, value: string) => ({ metric, unit, value, dimension: "", observed_at: null, is_final: null });

describe("spendThisMonth", () => {
  it("shows money per currency, never summed across currencies", () => {
    expect(spendThisMonth({ ...base, month_to_date_cost: [total("cost", "USD", "403.84"), total("cost", "INR", "80")] }))
      .toEqual({ text: expect.stringMatching(/\$403\.84 · .*80\.00/), muted: false });
  });
  it("shows credits as credits used, not money", () => {
    const line = spendThisMonth({ ...base, month_to_date_usage: [total("credits_used", "CREDITS", "902535")] });
    expect(line.text.replace(/,/g, "")).toMatch(/^902535 credits used$/); // grouping depends on locale
    expect(line.text).not.toMatch(/\$|₹/);
  });
  it("explains why there is no figure", () => {
    expect(spendThisMonth({ ...base, integration_status: "disabled" }).text).toBe("Not monitored (disabled)");
    expect(spendThisMonth({ ...base, integration_status: "not_configured" }).text).toBe("Needs configuration");
    expect(spendThisMonth({ ...base, spend_reported: false }).text).toBe("Not reported by provider");
    expect(spendThisMonth({ ...base, spend_reported: true }).text).toBe("No data yet");
    expect(spendThisMonth(undefined)).toEqual({ text: "—", muted: true });
  });
});
