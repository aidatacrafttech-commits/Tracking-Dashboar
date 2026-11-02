import { useHealthChecks, useProvider, useResources, useUsageSeries } from "../api/hooks";
import type { MetricTotal, Resource } from "../api/types";
import { TrendChart } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { useDisplayTimezone } from "../components/Layout";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Freshness,
  LoadingBlock,
  Notice,
  PageHeader,
  StatCard,
  StatusBadge,
} from "../components/ui";
import { formatAmount, formatDate, formatDateTime, formatMoney } from "../lib/format";
import { healthStatus, sourceStatus, syncStatus } from "../lib/status";

const str = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));

/** Values of a monthly metric for the current UTC month only (never last month's). */
export function currentMonth(items: MetricTotal[], metric: string, now: Date = new Date()): MetricTotal[] {
  const month = now.toISOString().slice(0, 7);
  return items.filter((m) => m.metric === metric && m.dimension === "" && m.period_start?.slice(0, 7) === month);
}

function Infrastructure() {
  const tz = useDisplayTimezone();
  const { data, error, isPending, refetch } = useResources();
  const usage = useUsageSeries("aws", { days: 7 });
  if (isPending) return <div className="card"><LoadingBlock rows={5} label="Loading AWS resources" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;

  const ec2 = data.items.filter((r) => r.resource_type === "ec2_instance");
  const ebs = data.items.filter((r) => r.resource_type === "ebs_volume");
  const running = ec2.filter((r) => r.state === "running").length;
  const impaired = ec2.filter((r) => r.health_status === "impaired").length;
  const totalGiB = ebs.reduce((sum, v) => sum + (Number(v.details?.size_gib) || 0), 0); // whole GiB counts, not money
  const s3 = (usage.data?.latest ?? []).filter((m) => m.metric.startsWith("s3_"));

  return (
    <section className="stack" aria-labelledby="infra-heading">
      <div className="section-title">
        <h2 id="infra-heading">Infrastructure health</h2>
        <Freshness iso={data.observed_at} stale={data.is_stale} label="Observed" />
      </div>
      <p className="muted small">Live state from the EC2 API and status checks. This is resource health, not billing.</p>
      {data.items.length === 0 ? (
        <div className="card">
          <EmptyState title="No resources recorded yet">They appear after the first successful AWS resources sync.</EmptyState>
        </div>
      ) : (
        <>
          <div className="grid grid-stats">
            <StatCard label="EC2 instances" value={ec2.length} sub={`${running} running`} />
            <StatCard label="Impaired instances" value={impaired} tone={impaired ? "critical" : undefined} sub="Failing a status check" />
            <StatCard label="EBS volumes" value={ebs.length} sub={`${totalGiB.toLocaleString()} GiB provisioned`} />
          </div>
          <Card title="EC2 instances">
            <DataTable<Resource>
              caption="EC2 instances"
              rows={ec2}
              rowKey={(r) => r.resource_id}
              empty={<EmptyState title="No EC2 instances in the monitored regions" />}
              columns={[
                { key: "name", header: "Name", render: (r) => r.name ?? "—" },
                { key: "id", header: "Instance", render: (r) => <code>{r.resource_id}</code> },
                { key: "region", header: "Zone", render: (r) => str(r.details?.availability_zone ?? r.region) },
                { key: "type", header: "Type", render: (r) => str(r.details?.instance_type) },
                { key: "state", header: "State", render: (r) => <StatusBadge status={r.state === "running" ? { tone: "normal", label: "Running" } : { tone: "unavailable", label: str(r.state) }} /> },
                {
                  key: "health",
                  header: "Status checks",
                  render: (r) =>
                    r.health_status ? (
                      <StatusBadge status={healthStatus(r.health_status)} title={`Instance: ${str(r.details?.instance_status)}, system: ${str(r.details?.system_status)}`} />
                    ) : (
                      <span className="muted">Not applicable</span>
                    ),
                },
                { key: "events", header: "Scheduled events", numeric: true, render: (r) => str(r.details?.scheduled_events) },
              ]}
            />
          </Card>
          <Card title="EBS volumes">
            <DataTable<Resource>
              caption="EBS volumes"
              rows={ebs}
              rowKey={(r) => r.resource_id}
              empty={<EmptyState title="No EBS volumes in the monitored regions" />}
              columns={[
                { key: "id", header: "Volume", render: (r) => <code>{r.resource_id}</code> },
                { key: "name", header: "Name", render: (r) => r.name ?? "—" },
                { key: "size", header: "Size", numeric: true, render: (r) => `${str(r.details?.size_gib)} GiB` },
                { key: "type", header: "Type", render: (r) => str(r.details?.volume_type) },
                { key: "state", header: "State", render: (r) => str(r.state) },
                { key: "attached", header: "Attached to", render: (r) => (Array.isArray(r.details?.attached_instances) && r.details.attached_instances.length ? (r.details.attached_instances as string[]).join(", ") : "Unattached") },
                { key: "enc", header: "Encrypted", render: (r) => (r.details?.encrypted ? <Badge tone="normal">Yes</Badge> : <Badge tone="warning">No</Badge>) },
              ]}
            />
          </Card>
        </>
      )}
      <Card title="S3 storage" description="Daily CloudWatch storage metrics (published once a day)">
        <DataTable
          caption="S3 storage"
          rows={s3}
          rowKey={(m) => `${m.metric}${m.dimension}`}
          empty={<EmptyState title="No S3 storage metrics yet" />}
          columns={[
            { key: "bucket", header: "Bucket / storage class", render: (m) => m.dimension },
            { key: "metric", header: "Metric", render: (m) => (m.metric === "s3_bucket_size_gb" ? "Size" : "Objects") },
            { key: "value", header: "Value", numeric: true, render: (m) => formatAmount(m.value, m.unit) },
            { key: "date", header: "As of", render: (m) => formatDate(m.observed_at, tz) },
          ]}
        />
      </Card>
    </section>
  );
}

function Billing() {
  const tz = useDisplayTimezone();
  const { data: provider } = useProvider("aws");
  const summary = useUsageSeries("aws", { days: 31 });
  const byService = useUsageSeries("aws", { days: 30, metric: "cost", by_dimension: true });
  const credits = provider?.latest_balance;
  const latest = summary.data?.latest ?? [];
  const mtd = currentMonth(latest, "cost_month");
  const forecast = currentMonth(latest, "cost_forecast_month");
  const dailyTotal = (summary.data?.series ?? []).filter((s) => s.metric === "cost");

  return (
    <section className="stack" aria-labelledby="billing-heading">
      <h2 id="billing-heading">Billing</h2>
      <Notice tone="info">
        Costs come from AWS Cost Explorer: billing data that refreshes at least once a day, not a live balance. Recent days are provisional until AWS finalises them.
      </Notice>
      <div className="grid grid-stats">
        <section className="card" aria-label="Month to date">
          <div className="stat-label">Month to date</div>
          {mtd.length ? mtd.map((m) => <div key={m.unit} className="stat-value">{formatMoney(m.value, m.unit)}</div>) : <div className="stat-value">—</div>}
          <div className="stat-sub">{mtd[0]?.observed_at ? `Fetched ${formatDateTime(mtd[0].observed_at, tz)}` : "No Cost Explorer data yet"}</div>
        </section>
        <section className="card" aria-label="Forecast for the month">
          <div className="stat-label">Forecast for the month</div>
          {forecast.length ? forecast.map((m) => <div key={m.unit} className="stat-value">{formatMoney(m.value, m.unit)}</div>) : <div className="stat-value">—</div>}
          <div className="stat-sub">AWS forecast plus month to date (an estimate)</div>
        </section>
        <section className="card" aria-label="Promotional credits remaining">
          <div className="stat-label">Promotional credits remaining</div>
          <div className="stat-value">{credits ? formatAmount(credits.amount, credits.unit) : "—"}</div>
          <div className="stat-sub row">
            {credits ? <StatusBadge status={sourceStatus(credits.source_type)} /> : "No credit data yet"}
          </div>
        </section>
      </div>
      <div className="grid grid-2">
        <Card title="Cost by service" description="Last 30 days, top services">
          {byService.isPending ? (
            <LoadingBlock rows={4} />
          ) : byService.error ? (
            <ErrorState error={byService.error} onRetry={() => byService.refetch()} />
          ) : byService.data.series.length === 0 ? (
            <EmptyState title="No per-service costs yet" />
          ) : (
            [...new Set(byService.data.series.map((s) => s.unit))].map((unit) => (
              <TrendChart key={unit} title={`Daily cost by service (${unit})`} kind="bar" unit={unit} series={byService.data.series.filter((s) => s.unit === unit)} labelOf={(s) => s.dimension} />
            ))
          )}
        </Card>
        <Card title="Daily total cost" description="Sum of all services per day">
          {summary.isPending ? (
            <LoadingBlock rows={4} />
          ) : dailyTotal.length === 0 ? (
            <EmptyState title="No daily costs yet" />
          ) : (
            dailyTotal.map((s) => <TrendChart key={s.unit} title={`Daily AWS cost (${s.unit})`} unit={s.unit} series={[s]} labelOf={() => `Total (${s.unit})`} />)
          )}
        </Card>
      </div>
    </section>
  );
}

export function AwsPage() {
  const tz = useDisplayTimezone();
  const { data: provider, isPending, error, refetch } = useProvider("aws");
  const health = useHealthChecks("aws");
  if (isPending) return <><PageHeader title="AWS monitoring" /><div className="card"><LoadingBlock rows={5} /></div></>;
  if (error) return <><PageHeader title="AWS monitoring" /><ErrorState error={error} onRetry={() => refetch()} /></>;
  const integration = provider.integration;
  const latest = health.data?.[0];
  return (
    <>
      <PageHeader
        title="AWS monitoring"
        description="Read-only: this dashboard never creates, stops, resizes or deletes AWS resources."
        actions={<StatusBadge status={syncStatus(integration?.status)} />}
      />
      {integration?.status === "disabled" && (
        <Notice tone="info">
          AWS monitoring is off. Set <code>AWS_MONITORING_ENABLED=true</code> with read-only credentials (see <code>docs/aws-monitoring-iam-policy.json</code>) and restart the server.
        </Notice>
      )}
      <Card title="Account access" description="Credential probe and EC2 status-check summary">
        {latest ? (
          <div className="row">
            <StatusBadge status={healthStatus(latest.status)} />
            <span>{latest.description ?? latest.error_message}</span>
            <span className="small muted">{formatDateTime(latest.checked_at, tz)}</span>
          </div>
        ) : (
          <p className="muted">No AWS checks yet.</p>
        )}
      </Card>
      <Infrastructure />
      <Billing />
    </>
  );
}
