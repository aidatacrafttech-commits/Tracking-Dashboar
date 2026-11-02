import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useAppSettings,
  useCreatePayment,
  useDeletePayment,
  usePayments,
  useProviders,
  useReconcile,
  useReconcilePreview,
  useUpdatePayment,
} from "../api/hooks";
import type { Payment, PaymentInput, PaymentKind } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { DataTable, Pagination, type SortState } from "../components/DataTable";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { IconPlus } from "../components/icons";
import { useDisplayTimezone } from "../components/Layout";
import { useToast } from "../components/Toast";
import { Amount, Badge, EmptyState, ErrorState, LoadingBlock, Notice, PageHeader } from "../components/ui";
import { formatAmount, formatDate, formatDateTime, formatMoney, titleCase } from "../lib/format";

const PAGE_SIZE = 25;
const KINDS: PaymentKind[] = ["top_up", "subscription", "invoice_payment", "refund", "other"];

function localDayStart(date: string, plusDays = 0): string {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return new Date(y, m - 1, d + plusDays).toISOString();
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// --- add / edit form ------------------------------------------------------------------------

interface FormState {
  provider: string;
  currency: string;
  amount: string;
  date: string;
  kind: PaymentKind;
  reference: string;
  notes: string;
}

function initialForm(editing: Payment | null, defaultProvider: string): FormState {
  return editing
    ? {
        provider: editing.provider,
        currency: editing.currency,
        amount: editing.amount,
        date: toLocalInput(editing.transaction_date),
        kind: editing.kind,
        reference: editing.payment_reference ?? "",
        notes: editing.notes ?? "",
      }
    : { provider: defaultProvider, currency: "USD", amount: "", date: toLocalInput(new Date().toISOString()), kind: "top_up", reference: "", notes: "" };
}

/** Mounted only while open (keyed), so its state always starts from the record. */
function PaymentFormDialog({
  editing,
  defaultProvider,
  onClose,
}: {
  editing: Payment | null;
  defaultProvider: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const { data: providers } = useProviders();
  const { data: settings } = useAppSettings();
  const create = useCreatePayment();
  const update = useUpdatePayment();
  const [form, setForm] = useState<FormState>(() => initialForm(editing, defaultProvider));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm({ ...form, [key]: value });
  const busy = create.isPending || update.isPending;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (!form.provider) next.provider = "Choose a provider.";
    if (!/^\d+(\.\d{1,2})?$/.test(form.amount.trim()) || Number(form.amount) <= 0) next.amount = "Enter a positive amount with at most 2 decimals, e.g. 50.00.";
    if (!form.date) next.transaction_date = "Choose the payment date.";
    else if (new Date(form.date).getTime() > Date.now() + 5 * 60_000) next.transaction_date = "The date cannot be in the future.";
    setErrors(next);
    if (Object.keys(next).length) return;
    const body: PaymentInput = {
      amount: form.amount.trim(),
      currency: form.currency,
      transaction_date: new Date(form.date).toISOString(),
      kind: form.kind,
      payment_reference: form.reference.trim() || null,
      notes: form.notes.trim() || null,
    };
    try {
      if (editing) await update.mutateAsync({ id: editing.id, body });
      else await create.mutateAsync({ provider: form.provider, body });
      toast.success(editing ? "Payment updated." : "Payment recorded. It does not change any balance by itself.");
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        const fields = err.fieldErrors();
        setErrors(Object.keys(fields).length ? fields : { form: err.message });
      } else setErrors({ form: "Could not save the payment." });
    }
  }

  const field = (id: string, label: string, control: React.ReactNode, key = id, hint?: string) => (
    <div className="field">
      <label htmlFor={`pf-${id}`}>{label}</label>
      {control}
      {hint && <span className="hint">{hint}</span>}
      {errors[key] && (
        <span className="error" id={`pf-${id}-error`} role="alert">
          {errors[key]}
        </span>
      )}
    </div>
  );

  return (
    <Dialog
      open
      onClose={onClose}
      title={editing ? "Edit payment" : "Record a payment"}
      description="Payments are kept as history. They never change a provider balance unless you explicitly apply them."
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form="payment-form" className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : editing ? "Save changes" : "Record payment"}
          </button>
        </>
      }
    >
      <form id="payment-form" onSubmit={submit} noValidate className="form-grid">
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}
        {field("provider", "Provider",
          <select id="pf-provider" value={form.provider} disabled={!!editing} onChange={(e) => set("provider", e.target.value)} aria-invalid={!!errors.provider}>
            <option value="">Choose…</option>
            {providers?.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
          </select>)}
        {field("kind", "Type",
          <select id="pf-kind" value={form.kind} onChange={(e) => set("kind", e.target.value as PaymentKind)}>
            {KINDS.map((k) => <option key={k} value={k}>{titleCase(k)}</option>)}
          </select>)}
        {field("amount", "Amount",
          <input id="pf-amount" type="text" inputMode="decimal" value={form.amount} onChange={(e) => set("amount", e.target.value)} aria-invalid={!!errors.amount} aria-describedby={errors.amount ? "pf-amount-error" : undefined} />)}
        {field("currency", "Currency",
          <select id="pf-currency" value={form.currency} onChange={(e) => set("currency", e.target.value)} aria-invalid={!!errors.currency}>
            {(settings?.supported_currencies ?? ["USD"]).map((c) => <option key={c}>{c}</option>)}
          </select>)}
        {field("date", "Payment date and time",
          <input id="pf-date" type="datetime-local" value={form.date} onChange={(e) => set("date", e.target.value)} aria-invalid={!!errors.transaction_date} aria-describedby={errors.transaction_date ? "pf-date-error" : undefined} />,
          "transaction_date", "Your local time")}
        {field("reference", "Reference",
          <input id="pf-reference" type="text" maxLength={128} value={form.reference} onChange={(e) => set("reference", e.target.value)} aria-invalid={!!errors.payment_reference} />,
          "payment_reference", "Receipt or invoice number (optional)")}
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label htmlFor="pf-notes">Notes</label>
          <textarea id="pf-notes" maxLength={2000} value={form.notes} onChange={(e) => set("notes", e.target.value)} />
        </div>
      </form>
    </Dialog>
  );
}

// --- explicit reconciliation ------------------------------------------------------------------

function ReconcileDialog({ payment, onClose }: { payment: Payment | null; onClose: () => void }) {
  const toast = useToast();
  const tz = useDisplayTimezone();
  const preview = useReconcilePreview(payment?.id ?? null);
  const reconcile = useReconcile();
  const p = preview.data;

  async function confirm() {
    if (!payment || !p?.base) return;
    try {
      const snapshot = await reconcile.mutateAsync({ id: payment.id, baseSnapshotId: p.base.snapshot_id });
      toast.success(`Balance updated to ${formatAmount(snapshot.amount, snapshot.unit)}.`);
      onClose();
    } catch (err) {
      toast.error(err);
      void preview.refetch();
    }
  }

  return (
    <Dialog
      open={!!payment}
      onClose={onClose}
      title="Apply payment to balance"
      description="This records a new manual balance. Only do this once you have confirmed the top-up reached the provider account."
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={confirm} disabled={!p?.allowed || reconcile.isPending}>
            {reconcile.isPending ? "Applying…" : "Apply to balance"}
          </button>
        </>
      }
    >
      {preview.isPending ? (
        <LoadingBlock rows={3} />
      ) : preview.error ? (
        <ErrorState error={preview.error} />
      ) : p && !p.allowed ? (
        <Notice tone="warning">{p.reason}</Notice>
      ) : p?.base && p.unit ? (
        <dl className="kv">
          <dt>Current balance</dt>
          <dd>
            {formatMoney(p.base.amount, p.unit)}{" "}
            <span className="small muted">({titleCase(p.base.source)}, {formatDateTime(p.base.observed_at, tz)})</span>
          </dd>
          <dt>This payment</dt>
          <dd>{formatMoney(p.payment_effect, p.unit)}</dd>
          <dt>New balance</dt>
          <dd><strong>{formatMoney(p.resulting_balance, p.unit)}</strong></dd>
        </dl>
      ) : null}
    </Dialog>
  );
}

// --- page ------------------------------------------------------------------------------------

export function PaymentsPage() {
  const tz = useDisplayTimezone();
  const toast = useToast();
  const { canWrite } = useAuth();
  const [params, setParams] = useSearchParams();
  const { data: providers } = useProviders();
  const { data: settings } = useAppSettings();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Payment | null>(null);
  const [deleting, setDeleting] = useState<Payment | null>(null);
  const [reconciling, setReconciling] = useState<Payment | null>(null);
  const del = useDeletePayment();

  const q = params.get("q") ?? "";
  const provider = params.get("provider") ?? "";
  const currency = params.get("currency") ?? "";
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const sort: SortState = { key: params.get("sort") ?? "date", order: (params.get("order") as "asc" | "desc") ?? "desc" };
  const offset = Number(params.get("offset") ?? 0) || 0;

  const update = (changes: Record<string, string>, resetOffset = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (resetOffset) next.delete("offset");
    setParams(next, { replace: true });
  };

  // Debounced free-text search.
  useEffect(() => {
    if (search === q) return;
    const id = window.setTimeout(() => update({ q: search }), 300);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const query = {
    q: q || undefined,
    provider: provider || undefined,
    currency: currency || undefined,
    start: from ? localDayStart(from) : undefined,
    end: to ? localDayStart(to, 1) : undefined,
    sort: sort.key,
    order: sort.order,
    limit: PAGE_SIZE,
    offset,
  };
  const { data, error, isPending, refetch, isPlaceholderData } = usePayments(query);
  const names = Object.fromEntries((providers ?? []).map((p) => [p.id, p.display_name]));
  const reconcilable = new Set((providers ?? []).filter((p) => p.balance_capability === "manual_only" || p.balance_capability === "partial").map((p) => p.id));

  async function confirmDelete() {
    if (!deleting) return;
    try {
      await del.mutateAsync(deleting.id);
      toast.success("Payment deleted. The deletion is recorded in the audit log.");
      setDeleting(null);
    } catch (err) {
      toast.error(err);
    }
  }

  return (
    <>
      <PageHeader
        title="Payments"
        description="Payment history across providers. Totals are per currency and never combined."
        actions={
          canWrite && (
            <button type="button" className="btn btn-primary" onClick={() => { setEditing(null); setFormOpen(true); }}>
              <IconPlus /> Record payment
            </button>
          )
        }
      />
      <Notice tone="info">
        A recorded payment is not proof that a provider balance increased. Balances change only when you use{" "}
        <strong>Apply to balance</strong> on a payment, or enter a new balance on the provider page.
      </Notice>

      <section className="card" aria-label="Filters">
        <div className="filters">
          <div className="field">
            <label htmlFor="pay-search">Search</label>
            <input id="pay-search" type="search" placeholder="Reference, notes or provider" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="pay-provider">Provider</label>
            <select id="pay-provider" value={provider} onChange={(e) => update({ provider: e.target.value })}>
              <option value="">All providers</option>
              {providers?.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="pay-currency">Currency</label>
            <select id="pay-currency" value={currency} onChange={(e) => update({ currency: e.target.value })}>
              <option value="">All currencies</option>
              {(settings?.supported_currencies ?? []).map((c) => <option key={c}>{c}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="pay-from">From</label>
            <input id="pay-from" type="date" value={from} max={to || undefined} onChange={(e) => update({ from: e.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="pay-to">To</label>
            <input id="pay-to" type="date" value={to} min={from || undefined} onChange={(e) => update({ to: e.target.value })} />
          </div>
        </div>
      </section>

      {isPending ? (
        <div className="card"><LoadingBlock rows={6} label="Loading payments" /></div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <section className="stack" aria-busy={isPlaceholderData} aria-label="Payment results">
          {data.totals.length > 0 && (
            <div className="row" aria-label="Net totals for the current filters">
              <span className="small muted">Net paid (refunds subtracted):</span>
              {data.totals.map((t) => (
                <Badge key={t.currency} tone="info">
                  {formatMoney(t.net_amount, t.currency)} · {t.count} payment{t.count === 1 ? "" : "s"}
                </Badge>
              ))}
            </div>
          )}
          <DataTable
            caption="Payments"
            rows={data.items}
            rowKey={(p) => p.id}
            sort={sort}
            onSort={(key) => update({ sort: key, order: sort.key === key && sort.order === "desc" ? "asc" : "desc" })}
            empty={
              <div className="card">
                <EmptyState title={q || provider || currency || from || to ? "No payments match these filters" : "No payments recorded yet"} />
              </div>
            }
            columns={[
              { key: "date", header: "Date", sortable: true, render: (p) => formatDate(p.transaction_date, tz) },
              { key: "provider", header: "Provider", sortable: true, render: (p) => names[p.provider] ?? p.provider },
              { key: "kind", header: "Type", render: (p) => titleCase(p.kind) },
              { key: "amount", header: "Amount", sortable: true, numeric: true, render: (p) => <Amount value={p.kind === "refund" ? `-${p.amount}` : p.amount} unit={p.currency} /> },
              { key: "currency", header: "Currency", render: (p) => p.currency },
              { key: "ref", header: "Reference", render: (p) => p.payment_reference ?? p.external_id ?? "—" },
              { key: "notes", header: "Notes", render: (p) => p.notes ?? "—" },
              {
                key: "source",
                header: "Source",
                render: (p) => (
                  <span className="row">
                    {p.entry_method === "manual" ? <Badge tone="info">Manual</Badge> : <Badge tone="normal">Provider verified</Badge>}
                    {p.reconciled_at && <Badge tone="normal">Applied to balance</Badge>}
                  </span>
                ),
              },
              ...(canWrite
                ? [{
                    key: "actions",
                    header: "Actions",
                    render: (p: Payment) =>
                      p.editable ? (
                        <span className="row">
                          <button type="button" className="btn btn-sm" onClick={() => { setEditing(p); setFormOpen(true); }} aria-label={`Edit payment ${p.payment_reference ?? p.id}`}>Edit</button>
                          <button type="button" className="btn btn-sm" onClick={() => setDeleting(p)} aria-label={`Delete payment ${p.payment_reference ?? p.id}`}>Delete</button>
                          {!p.reconciled_at && reconcilable.has(p.provider) && (
                            <button type="button" className="btn btn-sm" onClick={() => setReconciling(p)}>Apply to balance</button>
                          )}
                        </span>
                      ) : (
                        <span className="small muted">Read-only</span>
                      ),
                  }]
                : []),
            ]}
          />
          <Pagination total={data.total} limit={PAGE_SIZE} offset={offset} onChange={(o) => update({ offset: String(o) }, false)} label="payments" />
        </section>
      )}

      {formOpen && (
        <PaymentFormDialog key={editing?.id ?? "new"} editing={editing} defaultProvider={provider} onClose={() => setFormOpen(false)} />
      )}
      <ReconcileDialog payment={reconciling} onClose={() => setReconciling(null)} />
      <ConfirmDialog
        open={!!deleting}
        title="Delete this payment?"
        confirmLabel="Delete payment"
        destructive
        busy={del.isPending}
        onCancel={() => setDeleting(null)}
        onConfirm={confirmDelete}
      >
        {deleting && (
          <p>
            {formatMoney(deleting.amount, deleting.currency)} to {names[deleting.provider] ?? deleting.provider} on{" "}
            {formatDate(deleting.transaction_date, tz)}
            {deleting.payment_reference ? ` (${deleting.payment_reference})` : ""} will be removed. A full copy is kept in the audit log.
            {deleting.reconciled_at && " The balance entry it created stays; correct the balance separately if needed."}
          </p>
        )}
      </ConfirmDialog>
    </>
  );
}
