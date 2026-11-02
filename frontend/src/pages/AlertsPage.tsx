import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAlertDetail, useAlerts, useProviders, useRetryAlert } from "../api/hooks";
import type { AlertEvent, DeliveryStatus } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { DataTable, Pagination } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { useDisplayTimezone } from "../components/Layout";
import { useToast } from "../components/Toast";
import { Amount, EmptyState, ErrorState, LoadingBlock, PageHeader, StatusBadge } from "../components/ui";
import { formatDateTime, titleCase } from "../lib/format";
import { deliveryStatus, SOURCE_LABELS } from "../lib/status";

const PAGE_SIZE = 25;
const STATUSES: DeliveryStatus[] = ["pending", "sending", "sent", "failed", "suppressed"];

function retryText(a: AlertEvent, tz: string): string {
  if (a.delivery_status === "pending" && a.next_attempt_at) return `Next attempt ${formatDateTime(a.next_attempt_at, tz)}`;
  if (a.delivery_status === "failed") return `Gave up after ${a.delivery_attempts} attempt${a.delivery_attempts === 1 ? "" : "s"}`;
  if (a.delivery_status === "sent" && a.sent_at) return `Sent ${formatDateTime(a.sent_at, tz)}`;
  return a.delivery_attempts ? `${a.delivery_attempts} attempt(s)` : "—";
}

function AlertDetailDialog({ id, onClose }: { id: number | null; onClose: () => void }) {
  const tz = useDisplayTimezone();
  const { data, isPending, error } = useAlertDetail(id);
  return (
    <Dialog open={id !== null} onClose={onClose} title="Alert details" wide>
      {isPending ? (
        <LoadingBlock rows={4} />
      ) : error ? (
        <ErrorState error={error} />
      ) : (
        <div className="stack">
          <p><strong>{data.subject}</strong></p>
          <h3>Delivery attempts</h3>
          <DataTable
            caption="Delivery attempts"
            rows={data.attempts}
            rowKey={(a) => a.attempt_number}
            empty={<p className="muted">{data.delivery_status === "suppressed" ? `Not emailed: ${data.last_error}` : "No attempts yet."}</p>}
            columns={[
              { key: "n", header: "#", render: (a) => a.attempt_number },
              { key: "when", header: "When", render: (a) => formatDateTime(a.started_at, tz) },
              { key: "outcome", header: "Outcome", render: (a) => titleCase(a.outcome) },
              { key: "err", header: "Error", render: (a) => (a.error_code ? `${a.error_code}: ${a.error_message ?? ""}` : "—") },
            ]}
          />
          {data.body_html && (
            <>
              <h3>Email content</h3>
              <iframe title="Alert email content" className="email-frame" sandbox="" srcDoc={data.body_html} />
            </>
          )}
        </div>
      )}
    </Dialog>
  );
}

export function AlertsPage() {
  const tz = useDisplayTimezone();
  const toast = useToast();
  const { canWrite } = useAuth();
  const [params, setParams] = useSearchParams();
  const [detail, setDetail] = useState<number | null>(null);
  const { data: providers } = useProviders();
  const retry = useRetryAlert();
  const provider = params.get("provider") ?? "";
  const status = params.get("status") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const { data, error, isPending, refetch } = useAlerts({
    provider: provider || undefined,
    status: status || undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const names = Object.fromEntries((providers ?? []).map((p) => [p.id, p.display_name]));

  const update = (changes: Record<string, string>, resetOffset = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (resetOffset) next.delete("offset");
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageHeader title="Alert history" description="Every low-balance, reminder, recovery and test alert, with its email delivery result." />
      <section className="card" aria-label="Filters">
        <div className="row">
          <div className="field" style={{ minWidth: 180 }}>
            <label htmlFor="alert-provider">Provider</label>
            <select id="alert-provider" value={provider} onChange={(e) => update({ provider: e.target.value })}>
              <option value="">All providers</option>
              {providers?.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
            </select>
          </div>
          <div className="field" style={{ minWidth: 180 }}>
            <label htmlFor="alert-status">Delivery status</label>
            <select id="alert-status" value={status} onChange={(e) => update({ status: e.target.value })}>
              <option value="">Any status</option>
              {STATUSES.map((s) => <option key={s} value={s}>{deliveryStatus(s).label}</option>)}
            </select>
          </div>
        </div>
      </section>
      {isPending ? (
        <div className="card"><LoadingBlock rows={6} label="Loading alerts" /></div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <>
          <DataTable
            caption="Alert history"
            rows={data.items}
            rowKey={(a) => a.id}
            empty={<div className="card"><EmptyState title="No alerts yet">Alerts are created when a fresh, eligible balance reaches its threshold.</EmptyState></div>}
            columns={[
              { key: "time", header: "Time", render: (a) => formatDateTime(a.occurred_at, tz) },
              { key: "provider", header: "Provider", render: (a) => (a.event_type === "test" ? "Test email" : names[a.provider] ?? a.provider) },
              { key: "type", header: "Event", render: (a) => titleCase(a.event_type) },
              { key: "balance", header: "Balance", numeric: true, render: (a) => (a.event_type === "test" ? "—" : <Amount value={a.observed_amount} unit={a.unit} />) },
              { key: "threshold", header: "Threshold", numeric: true, render: (a) => (a.event_type === "test" ? "—" : <Amount value={a.threshold_amount} unit={a.unit} />) },
              { key: "source", header: "Source", render: (a) => (a.source_type ? <span className="small">{SOURCE_LABELS[a.source_type]}</span> : "—") },
              { key: "status", header: "Delivery", render: (a) => <StatusBadge status={deliveryStatus(a.delivery_status)} title={a.last_error ?? undefined} /> },
              { key: "to", header: "Recipients", render: (a) => a.recipients?.join(", ") || "—" },
              { key: "retry", header: "Retries", render: (a) => <span className="small">{retryText(a, tz)}</span> },
              {
                key: "actions",
                header: "Actions",
                render: (a) => (
                  <span className="row">
                    <button type="button" className="btn btn-sm" onClick={() => setDetail(a.id)}>Details</button>
                    {canWrite && a.delivery_status === "failed" && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={retry.isPending}
                        onClick={async () => {
                          try {
                            await retry.mutateAsync(a.id);
                            toast.success("Alert re-queued for delivery.");
                          } catch (e) {
                            toast.error(e);
                          }
                        }}
                      >
                        Retry
                      </button>
                    )}
                  </span>
                ),
              },
            ]}
          />
          <Pagination total={data.total} limit={PAGE_SIZE} offset={offset} onChange={(o) => update({ offset: String(o) }, false)} label="alerts" />
        </>
      )}
      <AlertDetailDialog id={detail} onClose={() => setDetail(null)} />
    </>
  );
}
