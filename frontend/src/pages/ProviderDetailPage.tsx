import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useAppSettings,
  useBalanceHistory,
  useHealthChecks,
  useManualBalance,
  usePayments,
  useProvider,
  useSyncNow,
  useSyncStatus,
  useThreshold,
  useUsageSeries,
} from "../api/hooks";
import type { BalanceSnapshot, Provider, Series } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { TrendChart } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { IconRefresh } from "../components/icons";
import { useDisplayTimezone } from "../components/Layout";
import { useToast } from "../components/Toast";
import {
  Amount,
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Freshness,
  LoadingBlock,
  Notice,
  PageHeader,
  StatusBadge,
} from "../components/ui";
import { formatAge, formatAmount, formatDate, formatDateTime, formatSigned, isCurrency, titleCase } from "../lib/format";
import { alertStatus, healthStatus, SOURCE_LABELS, sourceStatus, syncStatus } from "../lib/status";

const RANGES = [7, 30, 90] as const;

function QuotaBar({ details }: { details: Record<string, unknown> }) {
  const limit = Number(details.credit_limit);
  const used = Number(details.credits_used);
  if (!Number.isFinite(limit) || !Number.isFinite(used) || limit <= 0) return null;
  const pct = Math.min(100, Math.max(0, (used / limit) * 100));
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="row small" style={{ justifyContent: "space-between" }}>
        <span>Plan credits used</span>
        <span>
          {formatAmount(String(details.credits_used), "CREDITS")} of {formatAmount(String(details.credit_limit), "CREDITS")}
        </span>
      </div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label="Plan credits used">
        <span style={{ width: `${pct}%` }} />
      </div>
      {typeof details.resets_at === "string" && <span className="small muted">Resets {formatDate(details.resets_at, "UTC")}</span>}
    </div>
  );
}

function BalanceCard({ provider, tz }: { provider: Provider; tz: string }) {
  const bal = provider.latest_balance;
  const fresh = provider.latest_balance_freshness;
  return (
    <Card title="Balance" description={provider.balance_capability_note}>
      {!bal ? (
        <EmptyState title="No balance recorded yet">
          {provider.balance_capability === "manual_only" ? "Enter the balance from the provider dashboard below." : "It appears after the first successful balance sync."}
        </EmptyState>
      ) : (
        <div className="stack">
          <div className="stat-value">{formatAmount(bal.amount, bal.unit)}</div>
          {bal.amount === null && bal.unavailable_reason && <Notice tone="unavailable">{bal.unavailable_reason}</Notice>}
          <div className="row">
            <StatusBadge status={sourceStatus(bal.source_type)} />
            {bal.data_latency === "delayed" && <Badge tone="info">Billing data (delayed)</Badge>}
            <Freshness iso={bal.data_as_of ?? bal.observed_at} stale={fresh?.is_stale} />
          </div>
          {fresh?.is_stale && (
            <Notice tone="warning">
              This value is older than {provider.stale_after_minutes} minutes, so it is shown for reference only and not used for alerts.
            </Notice>
          )}
          <dl className="kv small">
            <dt>Kind</dt>
            <dd>{titleCase(bal.balance_kind)}</dd>
            <dt>Observed</dt>
            <dd>{formatDateTime(bal.observed_at, tz)}</dd>
            {bal.data_as_of && (
              <>
                <dt>Data as of</dt>
                <dd>{formatDateTime(bal.data_as_of, tz)}</dd>
              </>
            )}
          </dl>
          {bal.details && "credit_limit" in bal.details && <QuotaBar details={bal.details} />}
        </div>
      )}
    </Card>
  );
}

function ThresholdCard({ provider }: { provider: Provider }) {
  const { data: t, isPending } = useThreshold(provider.id);
  return (
    <Card title="Threshold" actions={<Link to={`/thresholds#${provider.id}`} className="small">Edit</Link>}>
      {isPending ? (
        <LoadingBlock rows={3} />
      ) : !t ? (
        <EmptyState title="No threshold configured" action={<Link to={`/thresholds#${provider.id}`}>Configure a threshold</Link>} />
      ) : (
        <div className="stack">
          <div className="row">
            <span className="stat-value" style={{ fontSize: 22 }}>{formatAmount(t.amount, t.unit)}</span>
            <StatusBadge status={alertStatus(t.alert_state, t.last_evaluation_status)} />
            {!t.is_enabled && <Badge tone="unavailable">Disabled</Badge>}
          </div>
          {provider.threshold_headroom ? (
            <p className={provider.threshold_headroom.position === "below" ? "text-critical" : "text-normal"}>
              <strong>{formatSigned(provider.threshold_headroom.amount, provider.threshold_headroom.unit)}</strong>{" "}
              {provider.threshold_headroom.position === "below" ? "below" : provider.threshold_headroom.position === "at" ? "(exactly at)" : "above"} threshold
              {provider.threshold_headroom.is_stale && " (based on stale data)"}
            </p>
          ) : (
            <p className="muted">Headroom unknown: no balance in {t.unit} yet.</p>
          )}
          {t.last_evaluation_message && <p className="small muted">Last check: {t.last_evaluation_message}</p>}
          <dl className="kv small">
            <dt>Recovers above</dt>
            <dd>{formatAmount(t.recovery_boundary, t.unit)}</dd>
            <dt>Alert sources</dt>
            <dd>{t.eligible_sources.map((src) => SOURCE_LABELS[src]).join(", ")}</dd>
            <dt>Recipients</dt>
            <dd>{t.effective_recipients.join(", ") || "None configured"}</dd>
          </dl>
        </div>
      )}
    </Card>
  );
}

function UsageSection({ id }: { id: string }) {
  const [days, setDays] = useState<number>(30);
  const { data, error, isPending, refetch } = useUsageSeries(id, { days });
  const byMetric = new Map<string, Series[]>();
  for (const s of data?.series ?? []) byMetric.set(`${s.metric}|${s.unit}`, [...(byMetric.get(`${s.metric}|${s.unit}`) ?? []), s]);
  return (
    <section className="stack" aria-labelledby="usage-heading">
      <div className="section-title">
        <h2 id="usage-heading">Usage and cost history</h2>
        <div className="row" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button key={r} type="button" className={`btn btn-sm ${days === r ? "btn-primary" : ""}`} aria-pressed={days === r} onClick={() => setDays(r)}>
              {r} days
            </button>
          ))}
        </div>
      </div>
      {isPending ? (
        <div className="card"><LoadingBlock rows={4} /></div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <>
          <Card title="Current billing period" description="Month to date (UTC), summed from daily provider data">
            {data.month_to_date.length === 0 && data.latest.length === 0 ? (
              <EmptyState title="No usage reported this month yet" />
            ) : (
              <dl className="kv">
                {data.month_to_date.map((m) => (
                  <div key={`${m.metric}${m.unit}`} style={{ display: "contents" }}>
                    <dt>{titleCase(m.metric)}</dt>
                    <dd><strong>{formatAmount(m.value, m.unit)}</strong></dd>
                  </div>
                ))}
                {data.latest.map((m) => (
                  <div key={`${m.metric}${m.dimension}${m.unit}`} style={{ display: "contents" }}>
                    <dt>{titleCase(m.metric)}{m.dimension ? ` · ${m.dimension}` : ""}</dt>
                    <dd>
                      {formatAmount(m.value, m.unit)}{" "}
                      {m.is_final === false && <Badge tone="info">Provisional</Badge>}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </Card>
          {byMetric.size === 0 ? (
            <div className="card"><EmptyState title={`No usage data in the last ${days} days`} /></div>
          ) : (
            <div className="grid grid-2">
              {[...byMetric.entries()].map(([key, series]) => {
                const [metric, unit] = key.split("|") as [string, string];
                return (
                  <Card key={key} title={titleCase(metric)} description={isCurrency(unit) ? `Daily, ${unit}` : `Daily, ${unit}`} headingLevel={3}>
                    <TrendChart title={`${titleCase(metric)} per day`} series={series} unit={unit} kind={metric === "cost" ? "bar" : "line"} labelOf={() => titleCase(metric)} height={200} />
                  </Card>
                );
              })}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function BalanceHistory({ id }: { id: string }) {
  const { data, isPending } = useBalanceHistory(id, 90);
  const known = (data?.items ?? []).filter((b): b is BalanceSnapshot & { amount: string } => b.amount !== null);
  const units = [...new Set(known.map((b) => b.unit))];
  return (
    <Card title="Balance history" description="Last 90 days, every recorded value (provider, manual and estimated)">
      {isPending ? (
        <LoadingBlock rows={3} />
      ) : known.length < 2 ? (
        <EmptyState title="Not enough balance history yet" />
      ) : (
        units.map((unit) => {
          const points = known.filter((b) => b.unit === unit).reverse();
          const series: Series = {
            metric: "balance",
            unit,
            dimension: "",
            points: points.map((b) => ({ period_start: b.observed_at, period_end: b.observed_at, value: b.amount, is_final: true })),
          };
          return <TrendChart key={unit} title={`Balance history in ${unit}`} series={[series]} unit={unit} labelOf={() => `Balance (${unit})`} height={200} />;
        })
      )}
    </Card>
  );
}

function ManualBalanceForm({ provider }: { provider: Provider }) {
  const toast = useToast();
  const mutation = useManualBalance(provider.id);
  const { data: settings } = useAppSettings();
  // "partial" providers (ElevenLabs) report plan credits via the API; what needs manual
  // entry is the separate pay-as-you-go money balance.
  const payg = provider.balance_capability === "partial";
  const [amount, setAmount] = useState("");
  const [notes, setNotes] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [error, setError] = useState<string | null>(null);
  const unit = payg ? currency : provider.default_unit;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!/^-?\d+(\.\d{1,2})?$/.test(amount.trim()) && isCurrency(unit)) {
      setError(`Enter an amount like 42.50 (${unit}).`);
      return;
    }
    if (!isCurrency(unit) && !/^\d+$/.test(amount.trim())) {
      setError("Enter a whole number of credits.");
      return;
    }
    try {
      await mutation.mutateAsync({
        amount: amount.trim(),
        unit,
        notes: notes.trim() || undefined,
        ...(payg ? { balance_kind: "prepaid_balance" } : {}),
      });
      toast.success(`Recorded ${formatAmount(amount.trim(), unit)} for ${provider.display_name}.`);
      setAmount("");
      setNotes("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the balance.");
    }
  }

  return (
    <Card
      title={payg ? "Record pay-as-you-go balance" : "Record current balance"}
      description={`Copy the ${payg ? "prepaid (pay-as-you-go) money balance" : "balance"} shown in the ${provider.display_name} dashboard. It is labelled as manually entered.`}
    >
      <form onSubmit={submit} noValidate className="form-grid">
        {payg && (
          <div className="field">
            <label htmlFor="manual-currency">Currency</label>
            <select id="manual-currency" value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {(settings?.supported_currencies ?? ["USD"]).map((c) => <option key={c}>{c}</option>)}
            </select>
          </div>
        )}
        <div className="field">
          <label htmlFor="manual-amount">Balance ({unit})</label>
          <input id="manual-amount" type="text" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} aria-invalid={!!error} aria-describedby={error ? "manual-error" : undefined} />
          {error && <span id="manual-error" className="error" role="alert">{error}</span>}
        </div>
        <div className="field">
          <label htmlFor="manual-notes">Note (optional)</label>
          <input id="manual-notes" type="text" maxLength={500} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        <div className="field" style={{ justifyContent: "flex-end" }}>
          <button type="submit" className="btn btn-primary" disabled={mutation.isPending}>
            {mutation.isPending ? "Saving…" : "Save balance"}
          </button>
        </div>
      </form>
    </Card>
  );
}

export function ProviderDetailPage() {
  const { id = "" } = useParams();
  const tz = useDisplayTimezone();
  const { canWrite } = useAuth();
  const toast = useToast();
  const { data: provider, error, isPending, refetch } = useProvider(id);
  const sync = useSyncStatus(id);
  const health = useHealthChecks(id);
  const payments = usePayments({ provider: id, limit: 10 });
  const syncNow = useSyncNow(id);

  if (isPending) return <><PageHeader title="Provider" /><div className="card"><LoadingBlock rows={5} /></div></>;
  if (error) {
    if (error instanceof ApiError && error.status === 404)
      return <><PageHeader title="Provider not found" /><div className="card"><EmptyState title={`No provider "${id}"`} action={<Link to="/providers">All providers</Link>} /></div></>;
    return <><PageHeader title="Provider" /><ErrorState error={error} onRetry={() => refetch()} /></>;
  }

  async function runSync() {
    try {
      const run = await syncNow.mutateAsync();
      if (run.skipped) toast.info(run.reason ?? "Sync skipped.");
      else if (run.status === "succeeded") toast.success(`${provider!.display_name} synced (${run.records_written} records).`);
      else toast.error(null, `Sync ${run.status}: ${run.operations.filter((o) => o.error_code).map((o) => `${o.operation} ${o.error_code}`).join(", ")}`);
    } catch (err) {
      toast.error(err);
    }
  }

  const integration = provider.integration;
  const latestHealth = health.data?.[0];
  return (
    <>
      <PageHeader
        title={provider.display_name}
        description={<Link to="/providers">← All providers</Link>}
        actions={
          canWrite && integration?.status === "ok" ? (
            <button type="button" className="btn" onClick={runSync} disabled={syncNow.isPending}>
              <IconRefresh /> {syncNow.isPending ? "Syncing…" : "Sync now"}
            </button>
          ) : undefined
        }
      />
      {integration?.status === "not_configured" && (
        <Notice tone="warning">
          This integration is not configured. Set {integration.missing_config.map((n) => <code key={n}>{n} </code>)} in the server&apos;s <code>.env</code> and restart. Values entered manually still work.
        </Notice>
      )}
      {integration?.status === "disabled" && <Notice tone="info">{integration.note ?? "This integration is disabled."}</Notice>}

      <div className="grid grid-3">
        <BalanceCard provider={provider} tz={tz} />
        <ThresholdCard provider={provider} />
        <Card title="Service status" description={provider.status_page_url ? undefined : "Provider health"}>
          {health.isPending ? (
            <LoadingBlock rows={2} />
          ) : !latestHealth ? (
            <EmptyState title="No health checks yet" />
          ) : (
            <div className="stack">
              <StatusBadge status={healthStatus(latestHealth.status)} />
              {latestHealth.description && <p>{latestHealth.description}</p>}
              {latestHealth.error_message && <p className="small text-critical">{latestHealth.error_message}</p>}
              <p className="small muted">
                Checked {formatAge(latestHealth.checked_at)} via {latestHealth.source === "status_page" ? "public status page" : "API probe"}
              </p>
              {provider.status_page_url && (
                <a href={provider.status_page_url} target="_blank" rel="noreferrer noopener" className="small">
                  Open status page
                </a>
              )}
            </div>
          )}
        </Card>
      </div>

      {canWrite && provider.balance_capability !== "api" && provider.balance_capability !== "postpaid" && <ManualBalanceForm provider={provider} />}

      <UsageSection id={id} />
      <BalanceHistory id={id} />

      <Card title="Sync status" description="Last outcome of each operation. Errors are summarised safely; credentials are never shown.">
        {sync.isPending ? (
          <LoadingBlock rows={4} />
        ) : sync.error ? (
          <ErrorState error={sync.error} onRetry={() => sync.refetch()} />
        ) : (
          <DataTable
            caption="Sync status by operation"
            rows={sync.data.operations}
            rowKey={(o) => o.operation}
            columns={[
              { key: "op", header: "Operation", render: (o) => titleCase(o.operation) },
              { key: "status", header: "Status", render: (o) => (o.supported ? <StatusBadge status={syncStatus(o.status)} /> : <Badge tone="unavailable">Not available</Badge>) },
              { key: "last", header: "Last success", render: (o) => (o.supported ? <Freshness iso={o.last_successful_sync} stale={o.is_stale} label="" /> : "—") },
              { key: "fail", header: "Failures", numeric: true, render: (o) => o.consecutive_failures || "—" },
              { key: "msg", header: "Details", render: (o) => (o.error_message ? <span className={o.status === "error" ? "text-critical" : "muted"}>{o.error_message}</span> : "—") },
            ]}
          />
        )}
      </Card>

      <Card title="Payments" actions={<Link to={`/payments?provider=${id}`} className="small">Manage payments</Link>}>
        {payments.isPending ? (
          <LoadingBlock rows={3} />
        ) : payments.error ? (
          <ErrorState error={payments.error} onRetry={() => payments.refetch()} />
        ) : (
          <DataTable
            caption={`${provider.display_name} payments`}
            rows={payments.data.items}
            rowKey={(p) => p.id}
            empty={<EmptyState title="No payments recorded for this provider" />}
            columns={[
              { key: "date", header: "Date", render: (p) => formatDate(p.transaction_date, tz) },
              { key: "kind", header: "Type", render: (p) => titleCase(p.kind) },
              { key: "amount", header: "Amount", numeric: true, render: (p) => <Amount value={p.kind === "refund" ? `-${p.amount}` : p.amount} unit={p.currency} /> },
              { key: "ref", header: "Reference", render: (p) => p.payment_reference ?? p.external_id ?? "—" },
              { key: "src", header: "Source", render: (p) => (p.entry_method === "manual" ? "Manual" : "Provider verified") },
            ]}
          />
        )}
      </Card>

      {integration && (
        <Card title="What this integration can report" headingLevel={2}>
          <DataTable
            caption="Capabilities"
            rows={integration.capabilities}
            rowKey={(c) => c.operation}
            columns={[
              { key: "op", header: "Operation", render: (c) => titleCase(c.operation) },
              { key: "sup", header: "Available", render: (c) => (c.supported ? <Badge tone="normal">Yes</Badge> : <Badge tone="unavailable">No</Badge>) },
              { key: "note", header: "Source / reason", render: (c) => c.note },
            ]}
          />
        </Card>
      )}
    </>
  );
}
