import { useMemo } from "react";
import { DataTable, type Column } from "../../components/DataTable";
import { Badge, EmptyState } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { Copyable } from "./Copyable";
import { usePackages, useSubjects } from "./hooks";
import { downloadCsv, IST, redemptionDate, rupees, targetLabel } from "./shared";
import type { Redemption } from "./types";

const STATUS_TONE: Record<Redemption["status"], "normal" | "info" | "warning" | "unavailable"> = {
  completed: "normal", pending: "info", cancelled: "unavailable", refunded: "warning",
};
const ist = (iso: string | null | undefined) => (iso ? formatDateTime(iso, IST) : "—");

/** Subject / package names keyed by their ids (falls back to a readable key). */
export function useTargetNames(): Record<string, string> {
  const subjects = useSubjects();
  const packages = usePackages();
  return useMemo(
    () => Object.fromEntries([
      ...(subjects.data?.subjects ?? []).map((s) => [s.key, s.name]),
      ...(packages.data?.packages ?? []).map((p) => [p.key, p.name]),
    ]),
    [subjects.data, packages.data],
  );
}

/** Client-side search over who redeemed (name, email, phone, user id) plus any extra text. */
export function matchesRedemption(r: Redemption, needle: string, extra: string[] = []): boolean {
  const q = needle.trim().toLowerCase();
  if (!q) return true;
  const u = r.user;
  return [u?.name, u?.email, u?.phone, u?.id ?? r.user_id, ...extra].some((x) => !!x && x.toLowerCase().includes(q));
}

/** CSV of the given rows: code, name, email, phone, package/course, paid, status, date (IST). */
export function exportRedemptionsCsv<R extends Redemption>(filename: string, rows: R[], codeOf: (r: R) => string, names: Record<string, string>) {
  downloadCsv(
    filename,
    ["Code", "Name", "Email", "Phone", "Package/Course", "Paid (INR)", "Status", "Date (IST)"],
    rows.map((r) => [
      codeOf(r),
      r.user?.name ?? "Deleted account",
      r.user?.email ?? "",
      r.user?.phone ?? "",
      targetLabel(r.target_id, names),
      r.is_free ? "Free" : String(r.final_amount_inr),
      r.status,
      ist(redemptionDate(r)),
    ]),
  );
}

/** Who used a voucher, for what, and what they paid. */
export function RedemptionTable<R extends Redemption>({
  caption, rows, names, empty, codeColumn,
}: {
  caption: string;
  rows: R[];
  names: Record<string, string>;
  empty: string;
  codeColumn?: Column<R>;
}) {
  const columns: Column<R>[] = [
    ...(codeColumn ? [codeColumn] : []),
    { key: "user", header: "User", render: (r) => (
      <span className="stack" style={{ gap: 2 }}>
        <span>{r.user?.name ?? <span className="muted">Deleted account</span>}</span>
        <span className="small muted"><Copyable value={r.user?.id ?? r.user_id} label="User ID" /></span>
      </span>
    ) },
    { key: "email", header: "Email", render: (r) => (r.user?.email ? <span className="small"><Copyable value={r.user.email} label="Email" /></span> : "—") },
    { key: "phone", header: "Phone", render: (r) => (r.user?.phone ? <span className="small"><Copyable value={r.user.phone} label="Phone" /></span> : "—") },
    { key: "target", header: "Package / course", render: (r) => targetLabel(r.target_id, names) },
    { key: "original", header: "Original", numeric: true, render: (r) => rupees(r.original_amount_inr) },
    { key: "discount", header: "Discount", numeric: true, render: (r) => rupees(r.discount_inr) },
    { key: "paid", header: "Paid", numeric: true, render: (r) => (r.is_free ? <strong>Free</strong> : rupees(r.final_amount_inr)) },
    { key: "status", header: "Status", render: (r) => (
      <span className="stack" style={{ gap: 2 }}>
        <Badge tone={STATUS_TONE[r.status] ?? "info"}>{r.status}</Badge>
        {r.late && <Badge tone="warning" title="Completed after the reservation window">Late</Badge>}
      </span>
    ) },
    { key: "date", header: "Date (IST)", render: (r) => <span className="small">{ist(redemptionDate(r))}</span> },
  ];
  return <DataTable caption={caption} rows={rows} rowKey={(r) => r.id} columns={columns} empty={<EmptyState title={empty} />} />;
}
