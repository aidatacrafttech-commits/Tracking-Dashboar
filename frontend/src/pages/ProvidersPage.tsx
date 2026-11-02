import { Link } from "react-router-dom";
import { useOverview, useProviders } from "../api/hooks";
import type { OverviewProvider, Provider } from "../api/types";
import { useDisplayTimezone } from "../components/Layout";
import { Badge, ErrorState, Freshness, LoadingBlock, PageHeader, StatusBadge } from "../components/ui";
import { spendThisMonth } from "../lib/spend";
import { formatAmount, formatDateTime, formatSigned } from "../lib/format";
import { alertStatus, providerSyncBadge, sourceStatus } from "../lib/status";

const CAPABILITY_LABELS = {
  api: "Balance from provider API",
  partial: "Plan quota from API; prepaid balance manual",
  manual_only: "No balance API: manual entry",
  postpaid: "Postpaid: credits and billing data",
} as const;

function ProviderCard({ p, tz, sync }: { p: Provider; tz: string; sync?: OverviewProvider }) {
  const bal = p.latest_balance;
  const failing = sync?.failing_operations ?? [];
  const spend = spendThisMonth(sync);
  return (
    <Link to={`/providers/${p.id}`} className="card card-link">
      <div className="card-header">
        <div>
          <h2>{p.display_name}</h2>
          <p>{CAPABILITY_LABELS[p.balance_capability]}</p>
        </div>
        <StatusBadge status={providerSyncBadge(p.integration?.status, failing, sync?.last_error_code)} />
      </div>
      <div className="stat-label">Latest balance</div>
      <div className="stat-value">{bal ? formatAmount(bal.amount, bal.unit) : "Unknown"}</div>
      {!bal && failing.includes("balance") && (
        <p className="small text-critical">Balance request is failing{sync?.last_error_code ? `: ${sync.last_error_code.replace(/_/g, " ")}` : ""}</p>
      )}
      {!bal && p.balance_capability === "manual_only" && (
        <p className="small muted">No balance API: open this card and use <strong>Record current balance</strong>.</p>
      )}
      <div className="row small" style={{ marginTop: 6 }}>
        <StatusBadge status={sourceStatus(bal?.source_type)} />
        {bal && <Freshness iso={bal.data_as_of ?? bal.observed_at} stale={p.latest_balance_freshness?.is_stale} />}
      </div>
      <hr className="divider" />
      <dl className="kv small">
        <dt>Threshold</dt>
        <dd>
          {p.threshold ? (
            <span className="row">
              {formatAmount(p.threshold.amount, p.threshold.unit)}
              <StatusBadge status={alertStatus(p.threshold.alert_state, p.threshold.last_evaluation_status)} />
            </span>
          ) : (
            <Badge tone="unavailable">Not set</Badge>
          )}
        </dd>
        <dt>Headroom</dt>
        <dd className={p.threshold_headroom?.position === "below" ? "text-critical" : undefined}>
          {p.threshold_headroom ? formatSigned(p.threshold_headroom.amount, p.threshold_headroom.unit) : "—"}
          {p.threshold_headroom?.is_stale && <span className="small text-warning"> (stale data)</span>}
        </dd>
        <dt>Observed</dt>
        <dd>{formatDateTime(bal?.observed_at, tz)}</dd>
        <dt>Spend this month</dt>
        <dd className={spend.muted ? "muted" : undefined}>{spend.text}</dd>
      </dl>
      {p.integration?.status === "not_configured" && (
        <p className="small muted" style={{ marginTop: 8 }}>
          Needs: {p.integration.missing_config.join(", ")}
        </p>
      )}
    </Link>
  );
}

export function ProvidersPage() {
  const tz = useDisplayTimezone();
  const { data, error, isPending, refetch } = useProviders();
  // Sync outcome and month-to-date spend per provider (summed on the server).
  const overview = useOverview();
  const syncById = Object.fromEntries((overview.data?.providers ?? []).map((o) => [o.id, o]));
  return (
    <>
      <PageHeader title="Providers" description="Balances, thresholds and integration status for each provider" />
      {isPending ? (
        <div className="grid grid-cards">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="card">
              <LoadingBlock rows={4} />
            </div>
          ))}
        </div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <div className="grid grid-cards">
          {data.map((p) => (
            <ProviderCard key={p.id} p={p} tz={tz} sync={syncById[p.id]} />
          ))}
        </div>
      )}
    </>
  );
}
