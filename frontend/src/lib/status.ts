// One vocabulary for state colours across the app. Every badge also shows text (and an
// icon), so meaning never depends on colour alone.

import type {
  AlertState,
  DeliveryStatus,
  EvaluationStatus,
  HealthStatus,
  SourceType,
  SyncStatus,
} from "../api/types";

export type Tone = "normal" | "warning" | "critical" | "unavailable" | "info";

export interface StatusInfo {
  tone: Tone;
  label: string;
}

export function alertStatus(state: AlertState | null | undefined, evaluation?: EvaluationStatus | null): StatusInfo {
  if (evaluation === "stale") return { tone: "warning", label: "Stale data" };
  if (evaluation === "currency_mismatch" || evaluation === "incompatible_units")
    return { tone: "warning", label: "Cannot compare" };
  if (evaluation === "no_data") return { tone: "unavailable", label: "No balance yet" };
  if (evaluation === "disabled") return { tone: "unavailable", label: "Threshold off" };
  if (state === "breached") return { tone: "critical", label: "Low balance" };
  if (state === "ok") return { tone: "normal", label: "Above threshold" };
  return { tone: "unavailable", label: "Not evaluated" };
}

export function syncStatus(status: SyncStatus | null | undefined): StatusInfo {
  switch (status) {
    case "ok":
      return { tone: "normal", label: "Synced" };
    case "partial":
      return { tone: "warning", label: "Partial" };
    case "error":
      return { tone: "critical", label: "Failing" };
    case "not_configured":
      return { tone: "unavailable", label: "Not configured" };
    case "disabled":
      return { tone: "unavailable", label: "Disabled" };
    case "unsupported":
      return { tone: "unavailable", label: "Not available" };
    default:
      return { tone: "unavailable", label: "Never run" };
  }
}

export function healthStatus(status: HealthStatus | string | null | undefined): StatusInfo {
  switch (status) {
    case "operational":
    case "ok":
      return { tone: "normal", label: "Operational" };
    case "degraded":
    case "maintenance":
      return { tone: "warning", label: status === "degraded" ? "Degraded" : "Maintenance" };
    case "partial_outage":
      return { tone: "critical", label: "Partial outage" };
    case "major_outage":
    case "impaired":
      return { tone: "critical", label: status === "impaired" ? "Impaired" : "Major outage" };
    default:
      return { tone: "unavailable", label: status ? String(status).replace(/[-_]/g, " ") : "Unknown" };
  }
}

export function deliveryStatus(status: DeliveryStatus): StatusInfo {
  switch (status) {
    case "sent":
      return { tone: "normal", label: "Sent" };
    case "pending":
      return { tone: "info", label: "Queued" };
    case "sending":
      return { tone: "info", label: "Sending" };
    case "failed":
      return { tone: "critical", label: "Failed" };
    case "suppressed":
      return { tone: "unavailable", label: "Not emailed" };
  }
}

export const SOURCE_LABELS: Record<SourceType, string> = {
  api_verified: "Verified by provider API",
  manual: "Entered manually",
  usage_estimate: "Estimated from usage",
  unavailable: "Unavailable",
};

export function sourceStatus(source: SourceType | null | undefined): StatusInfo {
  if (!source) return { tone: "unavailable", label: "No data" };
  const tone: Tone = source === "api_verified" ? "normal" : source === "unavailable" ? "unavailable" : "info";
  return { tone, label: SOURCE_LABELS[source] };
}

/**
 * What a provider card's status badge should say. "Configured" is not "working": when the
 * integration is set up but any operation's last sync failed, say so (with the reason).
 */
export function providerSyncBadge(
  integration: SyncStatus | null | undefined,
  failingOperations: readonly string[] = [],
  lastErrorCode?: string | null,
): StatusInfo {
  if (integration && integration !== "ok") return syncStatus(integration);
  if (failingOperations.length) {
    const reason = lastErrorCode ? ` (${lastErrorCode.replace(/_/g, " ")})` : "";
    return { tone: "critical", label: `Failing: ${failingOperations.join(", ")}${reason}` };
  }
  return syncStatus(integration);
}
