import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ConfirmDialog } from "../components/Dialog";
import { useToast } from "../components/Toast";
import { Badge, Card, EmptyState, ErrorState, LoadingBlock, Notice, PageHeader, StatusBadge } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { NotFoundPage } from "./NotFoundPage";
import { Tabs } from "./CoursesPage";
import { previewLecture, previewResource, useCourse, useCourseAction, useCoursePreview, usePublishCheck, useUpdateCourse } from "./courses/api";
import { ContentBuilder } from "./courses/ContentBuilder";
import { CourseForm } from "./courses/CourseForm";
import { EnrollmentsPanel } from "./courses/EnrollmentsPanel";
import { CourseScope } from "./courses/FileField";
import { duration, IST, LANGUAGES, money, STATUS_INFO } from "./courses/shared";
import type { CourseDetail, Playback } from "./courses/types";

const TABS = [
  { id: "details", label: "Details" },
  { id: "content", label: "Content" },
  { id: "publish", label: "Publish check" },
  { id: "preview", label: "Student preview" },
  { id: "enrollments", label: "Enrollments" },
] as const;
type Action = "publish" | "unpublish" | "archive" | "unarchive";

export function CourseEditorPage() {
  const id = Number(useParams().id);
  const { canWrite } = useAuth();
  const toast = useToast();
  const { data: course, error, isPending, refetch } = useCourse(id);
  const update = useUpdateCourse();
  const action = useCourseAction();
  const [confirm, setConfirm] = useState<Action | null>(null);
  if (!Number.isInteger(id) || id < 1) return <NotFoundPage />;
  if (isPending) return <div className="card"><LoadingBlock rows={6} label="Loading course" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;

  async function run(a: Action) {
    try {
      await action.mutateAsync({ id, action: a });
      toast.success({ publish: "Course published.", unpublish: "Course moved back to draft.", archive: "Course archived.", unarchive: "Course restored as a draft." }[a]);
    } catch (e) {
      toast.error(e);
    } finally {
      setConfirm(null);
    }
  }

  const actions = canWrite && (
    <span className="row">
      {course.status === "draft" && <button type="button" className="btn btn-primary" onClick={() => setConfirm("publish")}>Publish</button>}
      {course.status === "published" && <button type="button" className="btn" onClick={() => setConfirm("unpublish")}>Unpublish</button>}
      {course.status !== "archived" ? <button type="button" className="btn" onClick={() => setConfirm("archive")}>Archive</button>
        : <button type="button" className="btn" onClick={() => setConfirm("unarchive")}>Restore as draft</button>}
    </span>
  );
  const messages: Record<Action, { title: string; body: string; label: string }> = {
    publish: { title: "Publish this course?", label: "Publish", body: course.launch_at && new Date(course.launch_at) > new Date()
      ? `It will appear to students and open for purchase on ${formatDateTime(course.launch_at, IST)}.` : "Students can see and buy it immediately." },
    unpublish: { title: "Unpublish this course?", label: "Unpublish", body: "It disappears from the student catalog. Not possible while students hold or await access: archive instead." },
    archive: { title: "Archive this course?", label: "Archive", body: "No new purchases or enrollments. Existing students keep access until their own expiry." },
    unarchive: { title: "Restore this course?", label: "Restore", body: "It becomes a draft again; publish it to reopen sales." },
  };

  return (
    <>
      <p className="small"><Link to="/courses">← All courses</Link></p>
      <PageHeader title={course.title} description={
        <span className="row" style={{ gap: 8 }}>
          <StatusBadge status={STATUS_INFO[course.display_status]} />
          <span>{course.is_free ? "Free" : money(course.effective_price_paise, course.currency)} · {course.validity_label} from each purchase · {course.lecture_count} lecture(s) · {course.active_enrollment_count} active student(s)</span>
        </span>} actions={actions} />
      <CourseScope.Provider value={course.id}>
      <Tabs tabs={TABS} label="Course editor">
        {(tab) => {
          if (tab === "details")
            return (
              <Card title="Course details" description={course.enrollment_count ? "Students already enrolled keep the validity and price they bought; changes apply to new purchases only." : undefined}>
                <CourseForm key={course.version} course={course} readOnly={!canWrite} submitLabel="Save changes" busy={update.isPending}
                  onSubmit={async (input) => {
                    await update.mutateAsync({ id, body: { ...input, version: course.version } });
                    toast.success("Course saved.");
                  }} />
              </Card>
            );
          if (tab === "content") return <ContentBuilder course={course} canWrite={canWrite} />;
          if (tab === "publish") return <PublishPanel course={course} />;
          if (tab === "preview") return <PreviewPanel courseId={id} />;
          return <EnrollmentsPanel courseId={id} />;
        }}
      </Tabs>
      </CourseScope.Provider>
      {confirm && (
        <ConfirmDialog open title={messages[confirm].title} confirmLabel={messages[confirm].label} busy={action.isPending}
          destructive={confirm === "archive"} onConfirm={() => run(confirm)} onCancel={() => setConfirm(null)}>
          <p>{messages[confirm].body}</p>
        </ConfirmDialog>
      )}
    </>
  );
}

function PublishPanel({ course }: { course: CourseDetail }) {
  const { data, error, isPending, refetch } = usePublishCheck(course.id);
  if (isPending) return <div className="card"><LoadingBlock rows={3} label="Checking the course" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
  const blocking = data.problems.filter((p) => p.blocking);
  const advice = data.problems.filter((p) => !p.blocking);
  return (
    <Card title="Ready to publish?" description="Publishing is refused until every required item is in place.">
      {data.ready ? <Notice tone="normal">Everything required is in place{course.status === "draft" ? ": you can publish." : "."}</Notice>
        : <Notice tone="critical">{blocking.length} item(s) must be fixed before publishing.</Notice>}
      {blocking.length > 0 && <ul>{blocking.map((p, i) => <li key={i}>{p.message}</li>)}</ul>}
      {advice.length > 0 && (<><h3>Suggestions</h3><ul>{advice.map((p, i) => <li key={i}>{p.message}</li>)}</ul></>)}
      <button type="button" className="btn btn-sm" onClick={() => refetch()}>Check again</button>
    </Card>
  );
}

function PreviewPanel({ courseId }: { courseId: number }) {
  const toast = useToast();
  const { data, error, isPending, refetch } = useCoursePreview(courseId);
  const [playing, setPlaying] = useState<Playback | null>(null);
  const [loading, setLoading] = useState(false);
  if (isPending) return <div className="card"><LoadingBlock rows={4} label="Loading preview" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;

  async function open(lectureId: number, language?: string) {
    setLoading(true);
    try {
      setPlaying(await previewLecture(courseId, lectureId, language));
    } catch (e) {
      toast.error(e);
    } finally {
      setLoading(false);
    }
  }
  async function download(resourceId: number) {
    try {
      window.open((await previewResource(courseId, resourceId)).url, "_blank", "noopener");
    } catch (e) {
      toast.error(e);
    }
  }
  const a = data.availability;
  return (
    <div className="grid grid-2">
      <Card title={data.title} description="What students see (drafts included here so you can check them before publishing).">
        {data.thumbnail_url && <img src={data.thumbnail_url} alt="" style={{ width: "100%", maxHeight: 200, objectFit: "cover", borderRadius: 8 }} />}
        <p>{data.description}</p>
        {data.goals && <><h3>What you'll learn</h3><p style={{ whiteSpace: "pre-line" }}>{data.goals}</p></>}
        <p><strong>{data.is_free ? "Free" : money(data.effective_price_paise, data.currency)}</strong>
          {!data.is_free && data.original_price_paise !== null && <> <s className="muted">{money(data.original_price_paise, data.currency)}</s></>} · {data.validity_label} of access from purchase</p>
        {!a.purchasable && <Notice tone="info">{a.reason === "not_launched" ? `Not launched yet${a.launch_at ? `: opens ${formatDateTime(a.launch_at, IST)}` : ""}.` : a.reason === "archived" ? "This course is archived: no new enrollments." : "Draft: students can't see it yet."}</Notice>}
        {data.playlists.length === 0 ? <EmptyState title="No content yet" /> : data.playlists.map((p) => (
          <section key={p.id} className="stack" aria-label={`Playlist ${p.title}`}>
            <h3>{p.title}</h3>
            {p.chapters.map((ch) => (
              <div key={ch.id}>
                <strong>{ch.title}</strong>
                <ul className="plain-list">
                  {ch.lectures.map((l) => (
                    <li key={l.id} className="content-row">
                      <span>{l.title} <span className="small muted">· {duration(l.duration_seconds)} · {l.languages.map((x) => LANGUAGES[x] ?? x).join(", ") || "no video"}</span>
                        {l.is_free_preview && <> <Badge tone="info">Free preview</Badge></>}</span>
                      <button type="button" className="btn btn-sm" disabled={!l.languages.length || loading} onClick={() => open(l.id)} aria-label={`Play ${l.title}`}>Play</button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </section>
        ))}
      </Card>
      <Card title={playing ? playing.title : "Player"} description={playing ? `Language: ${playing.language_name}` : "Choose a lecture to play it here."}>
        {playing ? (
          <div className="stack">
            <video key={playing.video_url} src={playing.video_url} controls preload="metadata" style={{ width: "100%" }}>
              {/* Subtitles for this language when uploaded. */}
              <track kind="captions" src={playing.subtitle_url ?? undefined} label={playing.language_name} default={!!playing.subtitle_url} />
            </video>
            <div className="field">
              <label htmlFor="pv-language">Language</label>
              <select id="pv-language" value={playing.language} onChange={(e) => open(playing.lecture_id, e.target.value)}>
                {playing.available_languages.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
              </select>
              <span className="hint">Available in: {playing.available_languages.map((l) => l.name).join(", ")}</span>
            </div>
            {playing.resources.length > 0 && (
              <ul className="plain-list">
                {playing.resources.map((r) => (
                  <li key={r.id} className="content-row"><span>{r.title}{!r.is_downloadable && <span className="small muted"> · view only for students</span>}</span>
                    <button type="button" className="btn btn-sm" onClick={() => download(r.id)}>Open</button></li>
                ))}
              </ul>
            )}
          </div>
        ) : <EmptyState title="Nothing playing" />}
      </Card>
    </div>
  );
}
