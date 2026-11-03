import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { DataTable, Pagination, type SortState } from "../../components/DataTable";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { IconPlus } from "../../components/icons";
import { useToast } from "../../components/Toast";
import { EmptyState, ErrorState, LoadingBlock, StatusBadge } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { useCourseAction, useCourses, useCreateCourse, useDeleteCourse, useTaxonomy } from "./api";
import { CourseForm } from "./CourseForm";
import { IST, istInputToIso, priceLabel, STATUS_INFO } from "./shared";
import type { Course, DisplayStatus, TaxonomyKind } from "./types";

const PAGE = 25;

export function CourseList() {
  const { canWrite } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const taxonomy = useTaxonomy();
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState({ class_id: "", stream_id: "", subject_id: "", status: "", pricing: "", from: "", to: "" });
  const [sort, setSort] = useState<SortState>({ key: "created", order: "desc" });
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Course | null>(null);
  const [archiving, setArchiving] = useState<Course | null>(null);
  const create = useCreateCourse();
  const remove = useDeleteCourse();
  const action = useCourseAction();
  const setFilter = (k: keyof typeof filters, v: string) => { setFilters((f) => ({ ...f, [k]: v })); setOffset(0); };
  const filtered = !!(q || Object.values(filters).some(Boolean));
  const { data, error, isPending, isFetching, refetch } = useCourses({
    q: q || undefined, class_id: filters.class_id || undefined, stream_id: filters.stream_id || undefined,
    subject_id: filters.subject_id || undefined, status: filters.status || undefined, pricing: filters.pricing || undefined,
    launch_from: filters.from ? istInputToIso(`${filters.from}T00:00`) ?? undefined : undefined,
    launch_to: filters.to ? istInputToIso(`${filters.to}T23:59`) ?? undefined : undefined,
    sort: sort.key, order: sort.order, limit: PAGE, offset,
  });
  const terms = (kind: TaxonomyKind) => (taxonomy.data ?? []).filter((t) => t.kind === kind);

  async function doDelete() {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.id);
      toast.success(`“${deleting.title}” deleted.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeleting(null);
    }
  }
  async function doArchive() {
    if (!archiving) return;
    try {
      await action.mutateAsync({ id: archiving.id, action: "archive" });
      toast.success(`“${archiving.title}” archived.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setArchiving(null);
    }
  }

  const termSelect = (kind: TaxonomyKind, label: string) => (
    <div className="field">
      <label htmlFor={`cl-${kind}`}>{label}</label>
      <select id={`cl-${kind}`} value={filters[`${kind}_id`]} onChange={(e) => setFilter(`${kind}_id`, e.target.value)}>
        <option value="">All</option>
        {terms(kind).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
      </select>
    </div>
  );

  return (
    <div className="stack">
      <h2 className="visually-hidden">Courses</h2>
      <section className="card" aria-label="Course filters">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "2 1 220px" }}>
            <label htmlFor="cl-q">Course name</label>
            <input id="cl-q" type="search" maxLength={100} placeholder="Search by name" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
          </div>
          {termSelect("class", "Class")}
          {termSelect("stream", "Stream")}
          {termSelect("subject", "Subject")}
          <div className="field">
            <label htmlFor="cl-status">Status</label>
            <select id="cl-status" value={filters.status} onChange={(e) => setFilter("status", e.target.value)}>
              <option value="">All</option>
              {(Object.keys(STATUS_INFO) as DisplayStatus[]).map((s) => <option key={s} value={s}>{STATUS_INFO[s].label}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="cl-pricing">Free / paid</label>
            <select id="cl-pricing" value={filters.pricing} onChange={(e) => setFilter("pricing", e.target.value)}>
              <option value="">All</option>
              <option value="free">Free</option>
              <option value="paid">Paid</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="cl-from">Launch from</label>
            <input id="cl-from" type="date" value={filters.from} onChange={(e) => setFilter("from", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="cl-to">Launch to</label>
            <input id="cl-to" type="date" value={filters.to} onChange={(e) => setFilter("to", e.target.value)} />
          </div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          {filtered && <button type="button" className="btn btn-sm" onClick={() => { setQ(""); setFilters({ class_id: "", stream_id: "", subject_id: "", status: "", pricing: "", from: "", to: "" }); setOffset(0); }}>Clear filters</button>}
          {canWrite && <button type="button" className="btn btn-primary" style={{ marginLeft: "auto" }} onClick={() => setCreating(true)}><IconPlus /> New course</button>}
        </div>
      </section>

      {isPending ? <div className="card"><LoadingBlock rows={6} label="Loading courses" /></div> : error ? <ErrorState error={error} onRetry={() => refetch()} /> : (
        <div aria-busy={isFetching}>
          <DataTable
            caption="Courses"
            rows={data.items}
            rowKey={(c) => c.id}
            sort={sort}
            onSort={(key) => { setSort((s) => ({ key, order: s.key === key && s.order === "asc" ? "desc" : "asc" })); setOffset(0); }}
            empty={<div className="card"><EmptyState title={filtered ? "No courses match these filters" : "No courses yet"}
              action={!filtered && canWrite ? <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>Create the first course</button> : undefined} /></div>}
            columns={[
              { key: "title", header: "Course", sortable: true, render: (c) => (
                <span className="row" style={{ gap: 10, flexWrap: "nowrap" }}>
                  {c.thumbnail?.url ? <img src={c.thumbnail.url} alt="" className="thumb" /> : <span className="thumb thumb-empty" aria-hidden="true" />}
                  <span><Link to={`/courses/${c.id}`}><strong>{c.title}</strong></Link>
                    <span className="small muted" style={{ display: "block" }}>{[c.class_name, c.stream_name, c.subject_name].filter(Boolean).join(" · ") || "No class / subject"}</span></span>
                </span>
              ) },
              { key: "status", header: "Status", render: (c) => <StatusBadge status={STATUS_INFO[c.display_status]} /> },
              { key: "price", header: "Price", render: (c) => priceLabel(c) },
              { key: "launch_at", header: "Launch (IST)", sortable: true, render: (c) => (c.launch_at ? formatDateTime(c.launch_at, IST) : "Not set") },
              { key: "validity", header: "Validity", render: (c) => c.validity_label },
              { key: "content", header: "Lectures", numeric: true, render: (c) => c.lecture_count },
              { key: "enrollments", header: "Enrollments", numeric: true, render: (c) => `${c.active_enrollment_count} active / ${c.enrollment_count}` },
              { key: "actions", header: "Actions", render: (c) => (
                <span className="actions-cell">
                  <button type="button" className="btn btn-sm" onClick={() => navigate(`/courses/${c.id}`)} aria-label={`${canWrite ? "Edit" : "View"} ${c.title}`}>{canWrite ? "Edit" : "View"}</button>
                  {canWrite && c.status !== "archived" && <button type="button" className="btn btn-sm" onClick={() => setArchiving(c)} aria-label={`Archive ${c.title}`}>Archive</button>}
                  {canWrite && <button type="button" className="btn btn-sm btn-danger" disabled={!c.can_delete}
                    title={c.can_delete ? undefined : "Courses with enrollments can be archived, not deleted"}
                    onClick={() => setDeleting(c)} aria-label={`Delete ${c.title}`}>Delete</button>}
                </span>
              ) },
            ]}
          />
          <Pagination total={data.total} limit={PAGE} offset={offset} onChange={setOffset} label="courses" />
        </div>
      )}

      {creating && (
        <Dialog open wide onClose={() => setCreating(false)} title="New course" description="The course starts as a draft. Add content, then publish.">
          <CourseForm course={null} submitLabel="Create draft" busy={create.isPending} onCancel={() => setCreating(false)}
            onSubmit={async (input) => {
              const course = await create.mutateAsync(input);
              toast.success(`Draft “${course.title}” created.`);
              setCreating(false);
              navigate(`/courses/${course.id}?tab=content`);
            }} />
        </Dialog>
      )}
      <ConfirmDialog open={!!deleting} title={`Delete “${deleting?.title ?? ""}”?`} confirmLabel="Delete course" destructive busy={remove.isPending}
        onConfirm={doDelete} onCancel={() => setDeleting(null)}>
        <p>The course and all its playlists, chapters and lectures are deleted permanently. Only courses that no student has enrolled in can be deleted.</p>
      </ConfirmDialog>
      <ConfirmDialog open={!!archiving} title={`Archive “${archiving?.title ?? ""}”?`} confirmLabel="Archive" busy={action.isPending}
        onConfirm={doArchive} onCancel={() => setArchiving(null)}>
        <p>New students can no longer buy or enroll. Students who already enrolled keep access until their own expiry date.</p>
      </ConfirmDialog>
    </div>
  );
}
