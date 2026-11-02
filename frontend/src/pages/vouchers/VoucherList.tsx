import { useMemo, useState } from "react";
import { DataTable, Pagination } from "../../components/DataTable";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { IconPlus } from "../../components/icons";
import { useToast } from "../../components/Toast";
import { Badge, EmptyState, ErrorState, LoadingBlock, StatusBadge } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { useAdminVouchers, useDeleteVoucher, usePackages, useRedemptions, useSetVoucherEnabled, useSubjects } from "./hooks";
import { CATEGORY_INFO, discountLabel, getVoucherDisplayStatus, isExhausted, IST, PREFIX, scopeLabel, STATUS_INFO, STATUSES } from "./shared";
import { Copyable } from "./Copyable";
import { exportRedemptionsCsv, matchesRedemption, RedemptionTable, useTargetNames } from "./RedemptionTable";
import type { Category, Voucher, VoucherStatus } from "./types";
import { VoucherFormDialog } from "./VoucherForm";

const PAGE = 50;
const ist = (iso: string | null | undefined) => (iso ? formatDateTime(iso, IST) : "—");
/** The code admins know: the original one for deleted vouchers ("CODE~deleted~id" in `code`). */
export const displayCode = (v: Voucher) => (v.deleted ? v.original_code ?? v.code : v.code);
const of = (used: number, limit: number | null) => `${used.toLocaleString()} / ${limit === null ? "∞" : limit.toLocaleString()}`;

/** Course or Live Tutor vouchers from the AdhyaapakAI backend. */
export function VoucherList({ category }: { category: Category }) {
  const toast = useToast();
  const prefix = PREFIX[category];
  const info = CATEGORY_INFO[category];
  const [q, setQ] = useState("");
  // "fully_used" is a client-side filter: never sent to the API (see below).
  const [status, setStatus] = useState<VoucherStatus | "fully_used" | "">("");
  const [showDeleted, setShowDeleted] = useState(false);
  const [skip, setSkip] = useState(0);
  const [formFor, setFormFor] = useState<Voucher | "new" | null>(null);
  const [deactivating, setDeactivating] = useState<Voucher | null>(null);
  const [deleting, setDeleting] = useState<Voucher | null>(null);
  const [history, setHistory] = useState<Voucher | null>(null);
  const subjects = useSubjects();
  const packages = usePackages();
  const toggle = useSetVoucherEnabled(prefix);
  const remove = useDeleteVoucher(prefix);
  const { data, error, isPending, isFetching, refetch } = useAdminVouchers(prefix, {
    // Exhausted vouchers always have backend status "active", so ask for those and filter
    // the loaded page on the client.
    status: status === "fully_used" ? "active" : status || undefined,
    q: q.trim() || undefined,
    include_deleted: showDeleted,
    limit: PAGE,
    skip,
  });
  const names: Record<string, string> = Object.fromEntries(
    category === "course"
      ? (subjects.data?.subjects ?? []).map((s) => [s.key, s.name])
      : (packages.data?.packages ?? []).map((p) => [p.key, p.name]),
  );
  const filtered = !!(q || status || showDeleted);
  const rows = data ? (status === "fully_used" ? data.vouchers.filter(isExhausted) : data.vouchers) : [];
  const reset = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setSkip(0); };

  async function setEnabled(v: Voucher, enabled: boolean) {
    try {
      const { voucher } = await toggle.mutateAsync({ id: v.id, enabled });
      toast.success(`${voucher.code} is now ${getVoucherDisplayStatus(voucher).label.toLowerCase()}.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeactivating(null);
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.id);
      toast.success(`${deleting.code} deleted.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="stack">
      <section className="card" aria-label={`${info.title} filters`}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "2 1 220px" }}>
            <label htmlFor={`vq-${category}`}>Search</label>
            <input id={`vq-${category}`} type="search" maxLength={64} placeholder="Code or title" value={q} onChange={(e) => reset(setQ)(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={`vs-${category}`}>Status</label>
            <select id={`vs-${category}`} value={status} onChange={(e) => reset(setStatus)(e.target.value as VoucherStatus | "fully_used" | "")}>
              <option value="">All statuses</option>
              {STATUSES.map((s) => <option key={s} value={s}>{STATUS_INFO[s].label}</option>)}
              <option value="fully_used">Fully used</option>
            </select>
          </div>
          <label className="checkbox" style={{ minHeight: 38 }}>
            <input type="checkbox" checked={showDeleted} onChange={(e) => reset(setShowDeleted)(e.target.checked)} />
            Show deleted
          </label>
          <button type="button" className="btn btn-primary" style={{ marginLeft: "auto" }} onClick={() => setFormFor("new")}>
            <IconPlus /> New {info.label} voucher
          </button>
        </div>
      </section>

      {isPending ? (
        <div className="card"><LoadingBlock rows={6} label={`Loading ${info.title.toLowerCase()}`} /></div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <div aria-busy={isFetching}>
          <DataTable
            caption={info.title}
            rows={rows}
            rowKey={(v) => v.id}
            empty={
              <div className="card">
                <EmptyState title={filtered ? `No ${info.title.toLowerCase()} match these filters` : `No ${info.title.toLowerCase()} yet`}
                  action={!filtered ? <button type="button" className="btn btn-primary" onClick={() => setFormFor("new")}>Create the first {info.label} voucher</button> : undefined} />
              </div>
            }
            columns={[
              { key: "code", header: "Code", render: (v) => (
                <span className="stack" style={{ gap: 2 }}>
                  <Copyable value={displayCode(v)} label="Code"><code>{displayCode(v)}</code></Copyable>
                  {v.deleted && <Badge tone="unavailable" title={`Deleted ${ist(v.deleted_at)}`}>Deleted</Badge>}
                </span>
              ) },
              { key: "title", header: "Title", render: (v) => v.title ?? <span className="muted">—</span> },
              { key: "discount", header: "Discount", render: (v) => discountLabel(v) },
              { key: "scope", header: info.scope, render: (v) => <span className="small">{scopeLabel(v, names)}</span> },
              { key: "start", header: "Starts (IST)", render: (v) => ist(v.starts_at) },
              { key: "end", header: "Ends (IST)", render: (v) => (v.ends_at ? ist(v.ends_at) : "No end") },
              { key: "status", header: "Status", render: (v) => {
                const d = getVoucherDisplayStatus(v);
                return <StatusBadge status={d} title={d.title ?? (d.key === "deleted" ? `Deleted ${ist(v.deleted_at)}` : d.key === "active" ? "Redeemable now" : undefined)} />;
              } },
              { key: "used", header: "Used", numeric: true, render: (v) => of(v.stats.total_redemptions, v.total_limit) },
              { key: "users", header: "Unique users", numeric: true, render: (v) => of(v.stats.unique_users, v.max_unique_users) },
              {
                key: "actions",
                header: "Actions",
                render: (v) => (
                  <span className="actions-cell">
                    {!v.deleted && (
                      <>
                        <button type="button" className="btn btn-sm" onClick={() => setFormFor(v)} aria-label={`Edit ${v.code}`}>Edit</button>
                        {v.enabled ? (
                          <button type="button" className="btn btn-sm" disabled={toggle.isPending} onClick={() => setDeactivating(v)} aria-label={`Deactivate ${v.code}`}>Deactivate</button>
                        ) : (
                          <button type="button" className="btn btn-sm" disabled={toggle.isPending} onClick={() => setEnabled(v, true)} aria-label={`Activate ${v.code}`}>Activate</button>
                        )}
                        <button type="button" className="btn btn-sm btn-danger" onClick={() => setDeleting(v)} aria-label={`Delete ${v.code}`}>Delete</button>
                      </>
                    )}
                    <button type="button" className="btn btn-sm" onClick={() => setHistory(v)} aria-label={`Redemptions of ${displayCode(v)}`}>Redemptions</button>
                  </span>
                ),
              },
            ]}
          />
          {status === "fully_used" && data.vouchers.length > 0 && (
            <p className="small muted" role="status">
              {rows.length} fully used of the {data.vouchers.length} active vouchers on this page.
            </p>
          )}
          <Pagination total={data.total} limit={PAGE} offset={skip} onChange={setSkip} label={status === "fully_used" ? "active vouchers" : "vouchers"} />
        </div>
      )}

      {formFor && <VoucherFormDialog category={category} editing={formFor === "new" ? null : formFor} onClose={() => setFormFor(null)} />}
      {history && <RedemptionsDrawer category={category} voucher={history} onClose={() => setHistory(null)} />}
      <ConfirmDialog open={!!deactivating} title={`Deactivate voucher ${deactivating?.code ?? ""}?`} confirmLabel="Deactivate"
        busy={toggle.isPending} onConfirm={() => deactivating && setEnabled(deactivating, false)} onCancel={() => setDeactivating(null)}>
        <p><strong><code>{deactivating?.code}</code></strong> stops working immediately. Redemptions already made are kept, and you can activate it again later.</p>
      </ConfirmDialog>
      <ConfirmDialog open={!!deleting} title={`Delete voucher ${deleting?.code ?? ""}?`} confirmLabel="Delete voucher" destructive
        busy={remove.isPending} onConfirm={confirmDelete} onCancel={() => setDeleting(null)}>
        <p>
          <strong><code>{deleting?.code}</code></strong> stops working and is hidden from the list (tick <em>Show deleted</em> to see it).
          Its redemption history is kept, and the code becomes free to use for a new voucher.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function RedemptionsDrawer({ category, voucher, onClose }: { category: Category; voucher: Voucher; onClose: () => void }) {
  const [skip, setSkip] = useState(0);
  const [q, setQ] = useState("");
  const code = displayCode(voucher);
  const names = useTargetNames();
  const { data, error, isPending, refetch } = useRedemptions(PREFIX[category], voucher.id, { limit: PAGE, skip });
  const rows = useMemo(() => (data?.redemptions ?? []).filter((r) => matchesRedemption(r, q)), [data, q]);
  return (
    <Dialog open drawer onClose={onClose} title={`Redemptions — ${code}`} description="Times in IST. Search and export cover the loaded page.">
      {isPending ? <LoadingBlock rows={4} label="Loading redemptions" /> : error ? <ErrorState error={error} onRetry={() => refetch()} /> : (
        <>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <p style={{ margin: 0 }}>
              <strong>{data.total.toLocaleString()} redemptions · {voucher.stats.unique_users.toLocaleString()} users</strong>
            </p>
            <div className="field" style={{ flex: "1 1 220px" }}>
              <label htmlFor="rdw-q">Search this page</label>
              <input id="rdw-q" type="search" placeholder="Name, email or phone" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <button type="button" className="btn" disabled={rows.length === 0}
              onClick={() => exportRedemptionsCsv(`redemptions-${code}.csv`, rows, () => code, names)}>
              Export CSV
            </button>
          </div>
          <RedemptionTable caption={`Redemptions of ${code}`} rows={rows} names={names}
            empty={data.redemptions.length ? "No redemptions match this search" : "No redemptions yet"} />
          <Pagination total={data.total} limit={PAGE} offset={skip} onChange={setSkip} label="redemptions" />
        </>
      )}
    </Dialog>
  );
}
