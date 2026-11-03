import { useState, type FormEvent } from "react";
import { useAuth } from "../../auth/AuthContext";
import { DataTable, Pagination } from "../../components/DataTable";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/Toast";
import { Badge, EmptyState, ErrorState, LoadingBlock, Notice, StatusBadge } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { useCancelEnrollment, useEnrollment, useEnrollments } from "./api";
import { ENROLLMENT_INFO, IST, istInputToIso, money } from "./shared";
import type { Enrollment, EnrollmentStatus } from "./types";

const PAGE = 25;
const ist = (iso: string | null) => (iso ? formatDateTime(iso, IST) : "—");
const EVENT_LABELS: Record<string, string> = {
  order_created: "Checkout started (payment pending)",
  payment_confirmed: "Payment confirmed: access started",
  duplicate_callback: "Duplicate payment notice ignored",
  enrolled_free: "Enrolled (free course)",
  expired: "Access expired",
  cancelled: "Cancelled",
  refunded: "Refunded: access ended",
};

function paymentLabel(e: Enrollment): string {
  if (e.source === "free") return "Free";
  if (e.status === "pending") return "Pending";
  if (e.status === "refunded") return "Refunded";
  return e.payment_id ? `Paid ${money(e.amount_paid_paise, e.currency)}` : "—";
}

/** All enrollments (or one course's). Validity and expiry are the values saved at purchase. */
export function EnrollmentsPanel({ courseId }: { courseId?: number }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<EnrollmentStatus | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const reset = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setOffset(0); };
  const { data, error, isPending, isFetching, refetch } = useEnrollments({
    course_id: courseId, q: q || undefined, status: status || undefined,
    purchased_from: from ? istInputToIso(`${from}T00:00`) ?? undefined : undefined,
    purchased_to: to ? istInputToIso(`${to}T23:59`) ?? undefined : undefined, limit: PAGE, offset,
  });
  return (
    <div className="stack">
      <h2 className="visually-hidden">Enrollments</h2>
      <section className="card" aria-label="Enrollment filters">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "2 1 220px" }}>
            <label htmlFor="en-q">Search</label>
            <input id="en-q" type="search" maxLength={100} placeholder="Student name or ID, payment or order ID, course" value={q} onChange={(e) => reset(setQ)(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="en-status">Status</label>
            <select id="en-status" value={status} onChange={(e) => reset(setStatus)(e.target.value as EnrollmentStatus | "")}>
              <option value="">All</option>
              {(Object.keys(ENROLLMENT_INFO) as EnrollmentStatus[]).map((s) => <option key={s} value={s}>{ENROLLMENT_INFO[s].label}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="en-from">Purchased from</label>
            <input id="en-from" type="date" value={from} onChange={(e) => reset(setFrom)(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="en-to">Purchased to</label>
            <input id="en-to" type="date" value={to} onChange={(e) => reset(setTo)(e.target.value)} />
          </div>
        </div>
      </section>
      {isPending ? <div className="card"><LoadingBlock rows={5} label="Loading enrollments" /></div> : error ? <ErrorState error={error} onRetry={() => refetch()} /> : (
        <div aria-busy={isFetching}>
          <DataTable
            caption="Enrollments"
            rows={data.items}
            rowKey={(e) => e.id}
            empty={<div className="card"><EmptyState title={q || status || from || to ? "No enrollments match these filters" : "No enrollments yet"}>
              Enrollments appear when the app confirms a payment or a student joins a free course.</EmptyState></div>}
            columns={[
              { key: "student", header: "Student", render: (e) => <code>{e.student_id}</code> },
              ...(courseId ? [] : [{ key: "course", header: "Course", render: (e: Enrollment) => e.course_title }]),
              { key: "purchased", header: "Purchase date", render: (e) => ist(e.purchased_at) },
              { key: "payment", header: "Payment", render: (e) => paymentLabel(e) },
              { key: "start", header: "Access start", render: (e) => ist(e.access_start_at) },
              { key: "validity", header: "Validity", render: (e) => e.validity_label },
              { key: "expiry", header: "Access expiry", render: (e) => ist(e.access_expires_at) },
              { key: "status", header: "Status", render: (e) => <StatusBadge status={ENROLLMENT_INFO[e.status]} /> },
              { key: "open", header: "Details", render: (e) => <button type="button" className="btn btn-sm" onClick={() => setOpen(e.id)} aria-label={`Details for ${e.student_id}`}>Details</button> },
            ]}
          />
          <Pagination total={data.total} limit={PAGE} offset={offset} onChange={setOffset} label="enrollments" />
        </div>
      )}
      {open !== null && <EnrollmentDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function EnrollmentDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { canWrite } = useAuth();
  const toast = useToast();
  const { data: e, error, isPending } = useEnrollment(id);
  const cancel = useCancelEnrollment();
  const [reason, setReason] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  async function submit(ev: FormEvent) {
    ev.preventDefault();
    if (reason.trim().length < 3) return setProblem("Give a reason (at least 3 characters).");
    try {
      await cancel.mutateAsync({ id, reason: reason.trim() });
      toast.success("Enrollment cancelled; the student's access has ended.");
      setReason("");
    } catch (err) {
      toast.error(err);
    }
  }
  return (
    <Dialog open drawer onClose={onClose} title="Enrollment details" description="Purchase and access history. Times in IST.">
      {isPending ? <LoadingBlock rows={4} /> : error ? <ErrorState error={error} /> : (
        <div className="stack">
          <div className="row"><StatusBadge status={ENROLLMENT_INFO[e.status]} />{e.source === "free" && <Badge tone="info">Free course</Badge>}</div>
          <dl className="kv">
            <dt>Student ID</dt><dd><code>{e.student_id}</code></dd>
            <dt>Course</dt><dd>{e.course_title}</dd>
            <dt>Purchase date</dt><dd>{ist(e.purchased_at)}</dd>
            <dt>Payment</dt><dd>{e.source === "free" ? "Free" : `${money(e.amount_paid_paise, e.currency)} of ${money(e.list_price_paise, e.currency)}${e.voucher_code ? ` (voucher ${e.voucher_code})` : ""}`}</dd>
            <dt>Provider / payment ID</dt><dd>{e.payment_provider ?? "—"} · {e.payment_id ?? "—"}</dd>
            <dt>Order ID</dt><dd>{e.order_id ?? "—"}</dd>
            <dt>Access start</dt><dd>{ist(e.access_start_at)}</dd>
            <dt>Validity (at purchase)</dt><dd>{e.validity_label}</dd>
            <dt>Access expiry</dt><dd>{ist(e.access_expires_at)}</dd>
            {e.status_reason && (<><dt>Reason</dt><dd>{e.status_reason}</dd></>)}
          </dl>
          <p className="small muted">The validity and expiry above were saved when the student bought. Later changes to the course do not change them.</p>
          <h3>History</h3>
          <ol className="timeline">
            {e.events.map((ev) => (
              <li key={ev.id}><strong>{EVENT_LABELS[ev.event] ?? ev.event}</strong> <span className="small muted">· {ist(ev.occurred_at)} · {ev.actor === "course_api" ? "App backend" : ev.actor === "owner" ? "Owner" : "System"}</span></li>
            ))}
          </ol>
          {canWrite && (e.status === "active" || e.status === "pending") && (
            <form onSubmit={submit} className="stack card" aria-label="Cancel enrollment">
              <h3 style={{ margin: 0 }}>Cancel this enrollment</h3>
              <Notice tone="warning">The student loses access immediately. For refunds, record the refund in the app's payment system so it reports it here.</Notice>
              <div className="field">
                <label htmlFor="en-reason">Reason</label>
                <input id="en-reason" type="text" maxLength={255} value={reason} onChange={(ev) => { setReason(ev.target.value); setProblem(null); }} aria-invalid={!!problem} />
                {problem && <span className="error" role="alert">{problem}</span>}
              </div>
              <div><button type="submit" className="btn btn-danger" disabled={cancel.isPending}>{cancel.isPending ? "Cancelling…" : "Cancel enrollment"}</button></div>
            </form>
          )}
        </div>
      )}
    </Dialog>
  );
}
