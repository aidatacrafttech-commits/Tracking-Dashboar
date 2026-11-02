import { Link } from "react-router-dom";
import { useOverview } from "../api/hooks";
import type { Overview, OverviewProvider, Series } from "../api/types";
import { TrendChart } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { IconRefresh } from "../components/icons";
import { useDisplayTimezone } from "../components/Layout";
import {
  Amount,
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Freshness,
  LoadingBlock,
  PageHeader,
  StatCard,
  StatusBadge,
} from "../components/ui";
import { formatAge, formatAmount, formatDate, formatDateTime, formatMoney, formatSigned, titleCase } from "../lib/format";
import { spendThisMonth } from "../lib/spend";
import { alertStatus, deliveryStatus, providerSyncBadge, sourceStatus } from "../lib/status";

function groupByUnit(series: Series[]): Map<string, Series[]> {
  const out = new Map<string, Series[]>();
  for (const s of series) out.set(s.unit, [...(out.get(s.unit) ?? []), s]);
  return out;
}

function ProviderTile({ p }: { p: OverviewProvider }) {
  const status = alertStatus(p.alert_state, p.evaluation_status);
  const spend = spendThisMonth(p);
  return (
    <Link to={`/providers/${p.id}`} className="card card-link">
      <div className="card-header">
        <h3>{p.display_name}</h3>
        {p.threshold ? <StatusBadge status={status} /> : <Badge tone="unavailable">No threshold</Badge>}
      </div>
      <div className="stat-value" style={{ fontSize: 22 }}>
        {p.balance !== null && p.balance_unit ? formatAmount(p.balance, p.balance_unit) : "Unknown"}
      </div>
      {p.balance === null && p.balance_capability === "manual_only" ? (
        <p className="small muted" style={{ marginTop: 6 }}>
          {p.display_name} offers no balance API. Open this card and use <strong>Record current balance</strong>.
        </p>
      ) : (
        <div className="row small" style={{ marginTop: 6 }}>
          <StatusBadge status={sourceStatus(p.balance_source_type)} />
          {p.balance_observed_at && <Freshness iso={p.balance_observed_at} stale={p.balance_is_stale} />}
        </div>
      )}
      <hr className="divider" />
      <dl className="kv small">
        <dt>Threshold</dt>
        <dd>{p.threshold && p.threshold_unit ? formatAmount(p.threshold, p.threshold_unit) : "Not set"}</dd>
        <dt>Headroom</dt>
        <dd className={p.headroom?.position === "below" ? "text-critical" : undefined}>
          {p.headroom ? formatSigned(p.headroom.amount, p.headroom.unit) : "—"}
          {p.headroom?.is_stale && <span className="small text-warning"> (stale data)</span>}
        </dd>
        <dt>Spend this month</dt>
        <dd className={spend.muted ? "muted" : undefined}>{spend.text}</dd>
        <dt>Integration</dt>
        <dd>
          <StatusBadge status={providerSyncBadge(p.integration_status, p.failing_operations, p.last_error_code)} />
        </dd>
        <dt>Last sync</dt>
        <dd>{formatAge(p.last_successful_sync)}</dd>
      </dl>
    </Link>
  );
}

function SpendCharts({ data, names }: { data: Overview; names: Record<string, string> }) {
  const byCurrency = groupByUnit(data.spend_trend);
  if (byCurrency.size === 0)
    return (
      <Card title="Spending trend" description="Daily provider-reported costs, last 30 days">
        <EmptyState title="No cost data yet">Costs appear after the first successful billing sync.</EmptyState>
      </Card>
    );
  return (
    <>
      {[...byCurrency.entries()].map(([currency, series]) => (
        <Card key={currency} title={`Spending trend (${currency})`} description="Daily provider-reported costs, last 30 days. One currency per chart.">
          <TrendChart title={`Daily spend in ${currency}`} series={series} unit={currency} kind="bar" labelOf={(s) => names[s.provider ?? ""] ?? s.provider ?? ""} />
        </Card>
      ))}
    </>
  );
}

function UsageCharts({ data, names }: { data: Overview; names: Record<string, string> }) {
  if (data.usage_trend.length === 0)
    return (
      <Card title="Usage trend" description="Headline usage per provider, last 30 days">
        <EmptyState title="No usage data yet">Usage appears after the first successful usage sync.</EmptyState>
      </Card>
    );
  return (
    <Card title="Usage trend" description="Headline usage per provider, last 30 days (each in its own unit)">
      <div className="grid grid-2">
        {data.usage_trend.map((s) => (
          <div key={`${s.provider}-${s.metric}`}>
            <h3 className="small">
              {names[s.provider ?? ""] ?? s.provider} · {titleCase(s.metric)}
            </h3>
            <TrendChart title={`${names[s.provider ?? ""]} ${s.metric}`} series={[s]} unit={s.unit} labelOf={() => titleCase(s.metric)} height={180} />
          </div>
        ))}
      </div>
    </Card>
  );
}

export function OverviewPage() {
  const tz = useDisplayTimezone();
  const { data, error, isPending, refetch, isFetching } = useOverview();

  const header = (
    <PageHeader
      title="Overview"
      description={data ? `Updated ${formatDateTime(data.generated_at, tz)}` : "Balances, spend and alerts across all providers"}
      actions={
        <button type="button" className="btn" onClick={() => refetch()} disabled={isFetching}>
          <IconRefresh /> {isFetching ? "Refreshing…" : "Refresh"}
        </button>
      }
    />
  );
  if (isPending)
    return (
      <>
        {header}
        <div className="grid grid-stats">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="card">
              <LoadingBlock rows={2} />
            </div>
          ))}
        </div>
      </>
    );
  if (error) return (<>{header}<ErrorState error={error} onRetry={() => refetch()} /></>);

  const names = Object.fromEntries(data.providers.map((p) => [p.id, p.display_name]));
  const c = data.counts;
  return (
    <>
      {header}
      <div className="grid grid-stats">
        <StatCard label="Monitored providers" value={c.providers} sub={c.not_configured ? `${c.not_configured} not configured` : "All configured"} />
        <StatCard label="Low balance" value={c.low_balance} tone={c.low_balance ? "critical" : undefined} sub={c.stale_balances ? `${c.stale_balances} with stale data` : "At or below threshold"} />
        <StatCard label="Integration failures" value={c.integration_failures} tone={c.integration_failures ? "critical" : undefined} sub="Providers with a failing sync" />
        <StatCard label="Last successful sync" value={formatAge(data.last_successful_sync)} sub={formatDateTime(data.last_successful_sync, tz)} />
        <section className="card" aria-label="AWS spend this month">
          <div className="stat-label">AWS month-to-date</div>
          {data.aws_month_to_date.length === 0 ? (
            <>
              <div className="stat-value">—</div>
              <div className="stat-sub">No Cost Explorer data yet</div>
            </>
          ) : (
            data.aws_month_to_date.map((m) => (
              <div key={m.unit} className="stat-value">
                {formatMoney(m.value, m.unit)}
              </div>
            ))
          )}
          {data.aws_month_forecast.map((m) => (
            <div key={m.unit} className="stat-sub">
              Forecast {formatMoney(m.value, m.unit)}
            </div>
          ))}
          {data.aws_month_to_date.length > 0 && <div className="stat-sub">Billing data, up to 24 h delayed</div>}
        </section>
      </div>

      <section aria-labelledby="providers-heading" className="stack">
        <div className="section-title">
          <h2 id="providers-heading">Providers</h2>
          <Link to="/providers" className="small">
            All provider details
          </Link>
        </div>
        <div className="grid grid-cards">
          {data.providers.map((p) => (
            <ProviderTile key={p.id} p={p} />
          ))}
        </div>
      </section>

      <div className="grid grid-2">
        <SpendCharts data={data} names={names} />
        <UsageCharts data={data} names={names} />
      </div>

      <div className="grid grid-2">
        <Card title="Recent payments" actions={<Link to="/payments" className="small">All payments</Link>}>
          <DataTable
            caption="Recent payments"
            rows={data.recent_payments}
            rowKey={(p) => p.id}
            empty={<EmptyState title="No payments recorded" />}
            columns={[
              { key: "date", header: "Date", render: (p) => formatDate(p.transaction_date, tz) },
              { key: "provider", header: "Provider", render: (p) => names[p.provider] ?? p.provider },
              { key: "amount", header: "Amount", numeric: true, render: (p) => <Amount value={p.kind === "refund" ? `-${p.amount}` : p.amount} unit={p.currency} /> },
              { key: "method", header: "Source", render: (p) => (p.entry_method === "manual" ? "Manual" : "Provider") },
            ]}
          />
        </Card>
        <Card title="Recent alerts" actions={<Link to="/alerts" className="small">Alert history</Link>}>
          <DataTable
            caption="Recent alerts"
            rows={data.recent_alerts}
            rowKey={(a) => a.id}
            empty={<EmptyState title="No alerts yet">Alerts appear when a balance reaches its threshold.</EmptyState>}
            columns={[
              { key: "time", header: "Time", render: (a) => formatDateTime(a.occurred_at, tz) },
              { key: "provider", header: "Provider", render: (a) => names[a.provider] ?? a.provider },
              { key: "type", header: "Event", render: (a) => titleCase(a.event_type) },
              { key: "balance", header: "Balance", numeric: true, render: (a) => <Amount value={a.observed_amount} unit={a.unit} /> },
              { key: "status", header: "Email", render: (a) => <StatusBadge status={deliveryStatus(a.delivery_status)} /> },
            ]}
          />
        </Card>
      </div>
    </>
  );
}
