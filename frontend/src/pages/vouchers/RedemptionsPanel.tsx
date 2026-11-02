import { useMemo, useState } from "react";
import { Pagination } from "../../components/DataTable";
import { ErrorState, LoadingBlock } from "../../components/ui";
import { Copyable } from "./Copyable";
import { useAllRedemptions, type RedemptionRow } from "./hooks";
import { exportRedemptionsCsv, matchesRedemption, RedemptionTable, useTargetNames } from "./RedemptionTable";
import { CATEGORY_INFO } from "./shared";
import type { Category, Redemption } from "./types";

const PAGE = 50;
const codeOf = (r: RedemptionRow) => (r.voucher.deleted ? r.voucher.original_code ?? r.voucher_code : r.voucher_code);

/** Who used which voucher code, and for what: every redemption across both categories. */
export function RedemptionsPanel() {
  const { data, error, isPending, isFetching, refetch } = useAllRedemptions();
  const names = useTargetNames();
  const [q, setQ] = useState("");
  const [category, setCategory] = useState<Category | "">("");
  const [status, setStatus] = useState<Redemption["status"] | "">("");
  const [offset, setOffset] = useState(0);
  const reset = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setOffset(0); };

  const rows = useMemo(
    () => (data ?? []).filter((r) =>
      (!category || r.category === category) &&
      (!status || r.status === status) &&
      matchesRedemption(r, q, [codeOf(r), r.order_id ?? ""])),
    [data, q, category, status],
  );

  return (
    <div className="stack">
      <section className="card" aria-label="Redemption filters">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "2 1 240px" }}>
            <label htmlFor="rd-q">Search</label>
            <input id="rd-q" type="search" placeholder="Code, name, email, phone or order ID" value={q} onChange={(e) => reset(setQ)(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="rd-category">Category</label>
            <select id="rd-category" value={category} onChange={(e) => reset(setCategory)(e.target.value as Category | "")}>
              <option value="">Course and Live Tutor</option>
              <option value="course">Course</option>
              <option value="live_tutor">Live Tutor</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="rd-status">Status</label>
            <select id="rd-status" value={status} onChange={(e) => reset(setStatus)(e.target.value as Redemption["status"] | "")}>
              <option value="">All</option>
              <option value="completed">Completed</option>
              <option value="pending">Pending</option>
              <option value="cancelled">Cancelled</option>
              <option value="refunded">Refunded</option>
            </select>
          </div>
          <div className="row" style={{ marginLeft: "auto" }}>
            <button type="button" className="btn" disabled={rows.length === 0}
              onClick={() => exportRedemptionsCsv("voucher-redemptions.csv", rows, codeOf, names)}>Export CSV</button>
            <button type="button" className="btn" onClick={() => refetch()} disabled={isFetching}>{isFetching ? "Refreshing…" : "Refresh"}</button>
          </div>
        </div>
      </section>

      {isPending ? <div className="card"><LoadingBlock rows={6} label="Loading redemptions" /></div> : error ? <ErrorState error={error} onRetry={() => refetch()} /> : (
        <div aria-busy={isFetching}>
          <RedemptionTable
            caption="Voucher redemptions"
            rows={rows.slice(offset, offset + PAGE)}
            names={names}
            empty={data.length ? "No redemptions match these filters" : "No voucher has been used yet"}
            codeColumn={{ key: "code", header: "Voucher code", render: (r) => (
              <span className="stack" style={{ gap: 2 }}>
                <Copyable value={codeOf(r)} label="Code"><code>{codeOf(r)}</code></Copyable>
                <span className="small muted">{CATEGORY_INFO[r.category].label}{r.voucher.deleted ? " · deleted" : ""}</span>
              </span>
            ) }}
          />
          <Pagination total={rows.length} limit={PAGE} offset={offset} onChange={setOffset} label="redemptions" />
        </div>
      )}
    </div>
  );
}
