// "Spend this month" for a provider card. Money only when the provider reports costs; credits
// stay credits (never converted); otherwise say plainly why there is no figure.

import type { OverviewProvider } from "../api/types";
import { formatAmount } from "./format";

export interface SpendLine {
  text: string;
  /** true when there is no figure, only an explanation */
  muted: boolean;
}

export function spendThisMonth(row: OverviewProvider | undefined): SpendLine {
  if (!row) return { text: "—", muted: true };
  if (row.month_to_date_cost?.length)
    return { text: row.month_to_date_cost.map((m) => formatAmount(m.value, m.unit)).join(" · "), muted: false };
  if (row.month_to_date_usage?.length)
    return { text: row.month_to_date_usage.map((m) => `${formatAmount(m.value, m.unit)} used`).join(" · "), muted: false };
  if (row.integration_status === "disabled") return { text: "Not monitored (disabled)", muted: true };
  if (row.integration_status === "not_configured") return { text: "Needs configuration", muted: true };
  if (row.spend_reported === false) return { text: "Not reported by provider", muted: true };
  return { text: "No data yet", muted: true };
}
