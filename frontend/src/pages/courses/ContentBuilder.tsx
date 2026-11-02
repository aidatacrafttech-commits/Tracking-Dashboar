import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { IconPlus } from "../../components/icons";
import { useToast } from "../../components/Toast";
import { Badge, EmptyState, Notice } from "../../components/ui";
import { useDeleteNode, useReorder, useSaveNode, type LectureBody } from "./api";
import { FileField } from "./FileField";
import { LectureEditor } from "./LectureEditor";
import { duration, LANGUAGES } from "./shared";
import { SortableList } from "./SortableList";
import type { Chapter, CourseDetail, Lecture, Playlist } from "./types";

type NodeKind = "playlist" | "chapter";
type Editing =
  | { kind: NodeKind; parentId: number; node: Playlist | Chapter | null }
  | { kind: "lecture-new"; chapterId: number }
  | null;

/** Course -> Playlist -> Chapter -> Lecture builder. Every change is saved immediately. */
export function ContentBuilder({ course, canWrite }: { course: CourseDetail; canWrite: boolean }) {
  const toast = useToast();
  const reorder = useReorder();
  const remove = useDeleteNode();
  const [editing, setEditing] = useState<Editing>(null);
  const [lectureId, setLectureId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState<{ path: string; label: string; detail: string } | null>(null);
  const lecture = lectureId === null ? null
    : course.playlists.flatMap((p) => p.chapters).flatMap((c) => c.lectures).find((l) => l.id === lectureId) ?? null;

  const order = async (path: string, ids: number[]) => {
    try {
      await reorder.mutateAsync({ path, ids });
    } catch (e) {
      toast.error(e);
    }
  };
  async function confirmDelete() {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.path);
      toast.success(`${deleting.label} deleted.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeleting(null);
    }
  }

  const lectureRow = (l: Lecture) => (
    <div className="content-row">
      <div>
        <strong>{l.title}</strong>{" "}
        {l.status === "published" ? <Badge tone="normal">Published</Badge> : <Badge tone="unavailable">Draft</Badge>}
        {l.is_free_preview && <> <Badge tone="info">Free preview</Badge></>}
        <div className="small muted">
          {duration(l.duration_seconds)} · {l.variants.length ? l.variants.map((v) => LANGUAGES[v.language] ?? v.language).join(", ") : "No video yet"}
          {l.resources.length ? ` · ${l.resources.length} file(s)` : ""}
        </div>
      </div>
      <span className="actions-cell">
        <button type="button" className="btn btn-sm" onClick={() => setLectureId(l.id)} aria-label={`Open lecture ${l.title}`}>{canWrite ? "Edit" : "View"}</button>
        {canWrite && <button type="button" className="btn btn-sm btn-danger" aria-label={`Delete lecture ${l.title}`}
          onClick={() => setDeleting({ path: `/courses/lectures/${l.id}`, label: `Lecture “${l.title}”`, detail: "Its language versions and study materials are removed too." })}>Delete</button>}
      </span>
    </div>
  );

  const chapterRow = (ch: Chapter) => (
    <details className="content-node" open>
      <summary>
        <span><strong>{ch.title}</strong> <span className="small muted">· {ch.lectures.length} lecture(s)</span></span>
        {canWrite && (
          <span className="actions-cell">
            <button type="button" className="btn btn-sm" onClick={() => setEditing({ kind: "chapter", parentId: 0, node: ch })} aria-label={`Edit chapter ${ch.title}`}>Edit</button>
            <button type="button" className="btn btn-sm btn-danger" aria-label={`Delete chapter ${ch.title}`}
              onClick={() => setDeleting({ path: `/courses/chapters/${ch.id}`, label: `Chapter “${ch.title}”`, detail: "All its lectures are removed too." })}>Delete</button>
          </span>
        )}
      </summary>
      {ch.lectures.length ? (
        <SortableList items={ch.lectures} label={(l) => `lecture ${l.title}`} disabled={!canWrite} render={lectureRow}
          onReorder={(ids) => order(`/courses/chapters/${ch.id}/lectures/order`, ids)} />
      ) : <p className="small muted">No lectures yet.</p>}
      {canWrite && <button type="button" className="btn btn-sm" onClick={() => setEditing({ kind: "lecture-new", chapterId: ch.id })}><IconPlus /> Add lecture</button>}
    </details>
  );

  const playlistRow = (p: Playlist) => (
    <details className="content-node playlist" open>
      <summary>
        <span><strong>{p.title}</strong> <span className="small muted">· {p.chapters.length} chapter(s)</span></span>
        {canWrite && (
          <span className="actions-cell">
            <button type="button" className="btn btn-sm" onClick={() => setEditing({ kind: "playlist", parentId: course.id, node: p })} aria-label={`Edit playlist ${p.title}`}>Edit</button>
            <button type="button" className="btn btn-sm btn-danger" aria-label={`Delete playlist ${p.title}`}
              onClick={() => setDeleting({ path: `/courses/playlists/${p.id}`, label: `Playlist “${p.title}”`, detail: "All its chapters and lectures are removed too." })}>Delete</button>
          </span>
        )}
      </summary>
      {p.description && <p className="small muted">{p.description}</p>}
      {p.chapters.length ? (
        <SortableList items={p.chapters} label={(c) => `chapter ${c.title}`} disabled={!canWrite} render={chapterRow}
          onReorder={(ids) => order(`/courses/playlists/${p.id}/chapters/order`, ids)} />
      ) : <p className="small muted">No chapters yet.</p>}
      {canWrite && <button type="button" className="btn btn-sm" onClick={() => setEditing({ kind: "chapter", parentId: p.id, node: null })}><IconPlus /> Add chapter</button>}
    </details>
  );

  return (
    <section className="stack" aria-label="Course content">
      {course.playlists.length ? (
        <SortableList items={course.playlists} label={(p) => `playlist ${p.title}`} disabled={!canWrite} render={playlistRow}
          onReorder={(ids) => order(`/courses/${course.id}/playlists/order`, ids)} />
      ) : (
        <div className="card"><EmptyState title="No content yet">Add a playlist, then chapters and lectures inside it.</EmptyState></div>
      )}
      {canWrite && (
        <div>
          <button type="button" className="btn btn-primary" onClick={() => setEditing({ kind: "playlist", parentId: course.id, node: null })}>
            <IconPlus /> Add playlist
          </button>
        </div>
      )}

      {editing && (editing.kind === "playlist" || editing.kind === "chapter") && (
        <NodeDialog kind={editing.kind} parentId={editing.parentId} node={editing.node} onClose={() => setEditing(null)} />
      )}
      {editing?.kind === "lecture-new" && (
        <NewLectureDialog chapterId={editing.chapterId} onClose={() => setEditing(null)} onCreated={(id) => { setEditing(null); setLectureId(id); }} />
      )}
      {lecture && <LectureEditor courseId={course.id} lecture={lecture} canWrite={canWrite} onClose={() => setLectureId(null)} />}
      <ConfirmDialog open={!!deleting} title={`Delete ${deleting?.label ?? ""}?`} confirmLabel="Delete" destructive busy={remove.isPending}
        onConfirm={confirmDelete} onCancel={() => setDeleting(null)}>
        <p>{deleting?.detail} This can't be undone. Students' enrollments and purchase history are not affected.</p>
      </ConfirmDialog>
    </section>
  );
}

function NodeDialog({ kind, parentId, node, onClose }: { kind: NodeKind; parentId: number; node: Playlist | Chapter | null; onClose: () => void }) {
  const toast = useToast();
  const save = useSaveNode<Playlist | Chapter>();
  const [title, setTitle] = useState(node?.title ?? "");
  const [description, setDescription] = useState(node?.description ?? "");
  const [thumb, setThumb] = useState(node?.thumbnail ? { id: node.thumbnail.id, name: node.thumbnail.filename } : null);
  const [error, setError] = useState<string | null>(null);
  const label = kind === "playlist" ? "Playlist" : "Chapter";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    if (!title.trim()) return setError(`Enter the ${kind} name.`);
    const path = node ? `/courses/${kind}s/${node.id}` : kind === "playlist" ? `/courses/${parentId}/playlists` : `/courses/playlists/${parentId}/chapters`;
    try {
      await save.mutateAsync({ create: !node, path, body: { title: title.trim(), description: description.trim() || null, thumbnail_asset_id: thumb?.id ?? null } });
      toast.success(`${label} saved.`);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : `Could not save the ${kind}.`);
    }
  }
  return (
    <Dialog open onClose={onClose} title={node ? `Edit ${kind}` : `Add ${kind}`}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="submit" form="node-form" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
      </>}>
      <form id="node-form" className="stack" onSubmit={submit} noValidate>
        {error && <Notice tone="critical">{error}</Notice>}
        <div className="field">
          <label htmlFor="node-title">{label} name</label>
          <input id="node-title" type="text" maxLength={160} value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="node-description">Description{kind === "chapter" ? " (optional)" : ""}</label>
          <textarea id="node-description" rows={3} maxLength={5000} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <FileField kind="image" label={`${label} thumbnail${kind === "chapter" ? " (optional)" : ""}`} value={thumb} onChange={setThumb} />
      </form>
    </Dialog>
  );
}

function NewLectureDialog({ chapterId, onClose, onCreated }: { chapterId: number; onClose: () => void; onCreated: (id: number) => void }) {
  const save = useSaveNode<Lecture>();
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    if (!title.trim()) return setError("Enter the lecture title.");
    const body: LectureBody = { title: title.trim(), description: null, thumbnail_asset_id: null, duration_seconds: null,
      is_free_preview: false, status: "draft", default_language: null };
    try {
      const lecture = await save.mutateAsync({ create: true, path: `/courses/chapters/${chapterId}/lectures`, body });
      onCreated(lecture.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not add the lecture.");
    }
  }
  return (
    <Dialog open onClose={onClose} title="Add lecture" description="Add videos (per language) and study materials next."
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="submit" form="new-lecture" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Adding…" : "Add lecture"}</button>
      </>}>
      <form id="new-lecture" className="stack" onSubmit={submit} noValidate>
        {error && <Notice tone="critical">{error}</Notice>}
        <div className="field">
          <label htmlFor="new-lecture-title">Lecture title</label>
          <input id="new-lecture-title" type="text" maxLength={160} value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
      </form>
    </Dialog>
  );
}
