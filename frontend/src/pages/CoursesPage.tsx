import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { ErrorState, PageHeader, Skeleton, StatCard } from "../components/ui";
import { money } from "./courses/shared";
import { useCourseSummary } from "./courses/api";
import { CourseList } from "./courses/CourseList";
import { EnrollmentsPanel } from "./courses/EnrollmentsPanel";
import { TaxonomyPanel } from "./courses/TaxonomyPanel";

/** WAI-ARIA tabs kept in ?tab= (shared by the course pages). */
export function Tabs<T extends string>({ tabs, label, children }: { tabs: readonly { id: T; label: string }[]; label: string; children: (tab: T) => ReactNode }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab = (tabs.some((t) => t.id === requested) ? requested : tabs[0]!.id) as T;
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const select = (id: T) => setParams((p) => { const next = new URLSearchParams(p); next.set("tab", id); return next; }, { replace: true });
  const onKeyDown = (e: KeyboardEvent) => {
    const i = tabs.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % tabs.length : e.key === "ArrowLeft" ? (i - 1 + tabs.length) % tabs.length : -1;
    if (next < 0) return;
    e.preventDefault();
    select(tabs[next]!.id);
    refs.current[tabs[next]!.id]?.focus();
  };
  return (
    <>
      <div className="tabs" role="tablist" aria-label={label}>
        {tabs.map((t) => (
          <button key={t.id} ref={(el) => { refs.current[t.id] = el; }} type="button" role="tab" id={`tab-${t.id}`} aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`} tabIndex={tab === t.id ? 0 : -1} className={`tab ${tab === t.id ? "active" : ""}`}
            onClick={() => select(t.id)} onKeyDown={onKeyDown}>{t.label}</button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>{children(tab)}</div>
    </>
  );
}

const TABS = [
  { id: "courses", label: "Courses" },
  { id: "enrollments", label: "Enrollments" },
  { id: "taxonomy", label: "Classes, streams & subjects" },
] as const;

function Summary() {
  const { data, error, isPending, refetch } = useCourseSummary();
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
  const n = (v: number) => v.toLocaleString();
  return (
    <div className="grid grid-stats" aria-busy={isPending} style={{ marginBottom: 16 }}>
      {isPending ? Array.from({ length: 8 }, (_, i) => <div key={i} className="card"><Skeleton height={44} /></div>) : (
        <>
          <StatCard label="Total courses" value={n(data.total_courses)} sub={data.scheduled ? `${n(data.scheduled)} scheduled to launch` : undefined} />
          <StatCard label="Published courses" value={n(data.published + data.scheduled)} sub={`${n(data.published)} live now`} />
          <StatCard label="Draft courses" value={n(data.draft)} />
          <StatCard label="Archived courses" value={n(data.archived)} />
          <StatCard label="Total enrollments" value={n(data.total_enrollments)} sub={data.pending_enrollments ? `${n(data.pending_enrollments)} awaiting payment` : undefined} />
          <StatCard label="Active enrollments" value={n(data.active_enrollments)} />
          <StatCard label="Expired enrollments" value={n(data.expired_enrollments)} />
          <StatCard label="Total course revenue" value={data.revenue.length ? data.revenue.map((r) => money(r.amount_paise, r.currency)).join(" + ") : money(0, "INR")}
            sub="Amounts actually paid; refunds excluded" />
        </>
      )}
    </div>
  );
}

export function CoursesPage() {
  return (
    <>
      <PageHeader title="Course Management" description="Courses, their content and languages, and every student's enrollment and access." />
      <Summary />
      <Tabs tabs={TABS} label="Course management">
        {(tab) => (tab === "courses" ? <CourseList /> : tab === "enrollments" ? <EnrollmentsPanel /> : <TaxonomyPanel />)}
      </Tabs>
    </>
  );
}
