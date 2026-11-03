import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { IconPlus } from "../../components/icons";
import { useToast } from "../../components/Toast";
import { Badge, Notice } from "../../components/ui";
import { previewLecture, useDeleteNode, useReorder, useSaveNode, type LectureBody, type ResourceBody, type VariantBody, COURSES_API } from "./api";
import { FileField } from "./FileField";
import { LANGUAGES } from "./shared";
import { SortableList } from "./SortableList";
import type { Lecture, Resource, Variant } from "./types";

const ref = (a: { id: number; filename: string } | null) => (a ? { id: a.id, name: a.filename } : null);

function apiMessage(err: unknown, fallback: string): Record<string, string> {
  if (err instanceof ApiError) {
    const fields = err.fieldErrors();
    return Object.keys(fields).length ? fields : { form: err.message };
  }
  return { form: fallback };
}

/** Everything about one lecture: details, language versions, study materials. */
export function LectureEditor({ courseId, lecture, canWrite, onClose }: { courseId: number; lecture: Lecture; canWrite: boolean; onClose: () => void }) {
  const toast = useToast();
  const save = useSaveNode<Lecture>();
  const remove = useDeleteNode();
  const reorder = useReorder();
  const [form, setForm] = useState({
    title: lecture.title,
    description: lecture.description ?? "",
    thumb: ref(lecture.thumbnail),
    minutes: lecture.duration_seconds ? String(Math.round(lecture.duration_seconds / 60)) : "",
    is_free_preview: lecture.is_free_preview,
    status: lecture.status,
    default_language: lecture.default_language ?? "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [variant, setVariant] = useState<Variant | "new" | null>(null);
  const [resource, setResource] = useState<Resource | "new" | null>(null);
  const [deleting, setDeleting] = useState<{ path: string; label: string } | null>(null);
  const [preview, setPreview] = useState<{ url: string; language: string } | null>(null);
  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    const found: Record<string, string> = {};
    if (!form.title.trim()) found.title = "Enter the lecture title.";
    if (form.minutes && (!/^\d+$/.test(form.minutes) || Number(form.minutes) > 1440)) found.duration_seconds = "Whole minutes, up to 1440.";
    if (form.status === "published" && lecture.variants.length === 0) found.status = "Add a video in at least one language before publishing this lecture.";
    setErrors(found);
    if (Object.keys(found).length) return;
    const body: LectureBody = {
      title: form.title.trim(), description: form.description.trim() || null, thumbnail_asset_id: form.thumb?.id ?? null,
      duration_seconds: form.minutes ? Number(form.minutes) * 60 : null, is_free_preview: form.is_free_preview,
      status: form.status, default_language: form.default_language || null,
    };
    try {
      await save.mutateAsync({ create: false, path: `${COURSES_API}/lectures/${lecture.id}`, body });
      toast.success("Lecture saved.");
    } catch (err) {
      setErrors(apiMessage(err, "Could not save the lecture."));
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.path);
      toast.success(`${deleting.label} removed.`);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeleting(null);
    }
  }

  async function watch(language: string) {
    try {
      const p = await previewLecture(courseId, lecture.id, language);
      setPreview({ url: p.video_url, language: p.language_name });
    } catch (e) {
      toast.error(e);
    }
  }

  const err = (id: string) => errors[id] && <span className="error" role="alert">{errors[id]}</span>;
  return (
    <Dialog open drawer onClose={onClose} title={`Lecture: ${lecture.title}`} description="Changes are saved per section.">
      <form className="form-grid" onSubmit={submit} noValidate aria-label="Lecture details">
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label htmlFor="le-title">Lecture title</label>
          <input id="le-title" type="text" maxLength={160} value={form.title} disabled={!canWrite} onChange={(e) => set("title", e.target.value)} aria-invalid={!!errors.title} />
          {err("title")}
        </div>
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label htmlFor="le-description">Lecture description</label>
          <textarea id="le-description" rows={3} maxLength={5000} value={form.description} disabled={!canWrite} onChange={(e) => set("description", e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="le-minutes">Duration (minutes)</label>
          <input id="le-minutes" type="number" min={0} max={1440} value={form.minutes} disabled={!canWrite} onChange={(e) => set("minutes", e.target.value)} aria-invalid={!!errors.duration_seconds} />
          {err("duration_seconds")}
        </div>
        <div className="field">
          <label htmlFor="le-status">Lecture status</label>
          <select id="le-status" value={form.status} disabled={!canWrite} onChange={(e) => set("status", e.target.value as Lecture["status"])} aria-invalid={!!errors.status}>
            <option value="draft">Draft (hidden from students)</option>
            <option value="published">Published</option>
          </select>
          {err("status")}
        </div>
        <div className="field">
          <label htmlFor="le-default">Default language</label>
          <select id="le-default" value={form.default_language} disabled={!canWrite || !lecture.variants.length} onChange={(e) => set("default_language", e.target.value)}>
            {!lecture.variants.length && <option value="">Add a language version first</option>}
            {lecture.variants.map((v) => <option key={v.id} value={v.language}>{v.language_name}</option>)}
          </select>
          {err("default_language")}
        </div>
        <div className="field">
          <span className="label">Access</span>
          <label className="checkbox"><input type="checkbox" checked={form.is_free_preview} disabled={!canWrite} onChange={(e) => set("is_free_preview", e.target.checked)} /> Free preview (anyone can watch)</label>
        </div>
        <div style={{ gridColumn: "1 / -1" }}>
          <FileField kind="image" label="Lecture thumbnail" value={form.thumb} disabled={!canWrite} onChange={(a) => set("thumb", a)} />
        </div>
        {canWrite && (
          <div style={{ gridColumn: "1 / -1" }}>
            <button type="submit" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save lecture"}</button>
          </div>
        )}
      </form>

      <section className="stack" aria-labelledby="le-variants">
        <h3 id="le-variants" className="form-section">Language versions</h3>
        <p className="small muted" style={{ margin: 0 }}>The same lecture in different languages. Students pick from the languages listed here.</p>
        {lecture.variants.length === 0 ? <Notice tone="warning">No video yet. Add at least one language version.</Notice> : (
          <ul className="plain-list">
            {lecture.variants.map((v) => (
              <li key={v.id} className="content-row">
                <div>
                  <strong>{v.language_name}</strong>{v.language === lecture.default_language && <> <Badge tone="info">Default</Badge></>}
                  <div className="small muted">
                    {v.video ? `Video: ${v.video.filename}${v.video.status === "pending" ? " (upload incomplete)" : ""}` : v.video_url ? `Video URL: ${v.video_url}` : "No video"}
                    {v.audio ? ` · Audio: ${v.audio.filename}` : ""}{v.subtitle ? ` · Subtitles: ${v.subtitle.filename}` : ""}
                  </div>
                </div>
                <span className="actions-cell">
                  <button type="button" className="btn btn-sm" onClick={() => watch(v.language)} aria-label={`Preview ${v.language_name} video`}>Preview</button>
                  {canWrite && <>
                    <button type="button" className="btn btn-sm" onClick={() => setVariant(v)} aria-label={`Edit ${v.language_name} version`}>Edit</button>
                    <button type="button" className="btn btn-sm btn-danger" onClick={() => setDeleting({ path: `${COURSES_API}/variants/${v.id}`, label: `${v.language_name} version` })}
                      aria-label={`Remove ${v.language_name} version`}>Remove</button>
                  </>}
                </span>
              </li>
            ))}
          </ul>
        )}
        {canWrite && <div><button type="button" className="btn btn-sm" onClick={() => setVariant("new")}><IconPlus /> Add language version</button></div>}
        {preview && (
          <figure style={{ margin: 0 }}>
            <figcaption className="small">Preview ({preview.language})</figcaption>
            <video src={preview.url} controls preload="metadata" style={{ width: "100%", maxHeight: 320 }}>
              <track kind="captions" />
            </video>
          </figure>
        )}
      </section>

      <section className="stack" aria-labelledby="le-resources">
        <h3 id="le-resources" className="form-section">Study materials</h3>
        {lecture.resources.length === 0 ? <p className="small muted">No PDFs or notes yet.</p> : (
          <SortableList items={lecture.resources} label={(r) => `material ${r.title}`} disabled={!canWrite}
            onReorder={(ids) => reorder.mutateAsync({ path: `${COURSES_API}/lectures/${lecture.id}/resources/order`, ids }).catch((e) => toast.error(e))}
            render={(r) => (
              <div className="content-row">
                <div><strong>{r.title}</strong> <span className="small muted">· {r.asset.filename}{r.is_downloadable ? "" : " · view only"}</span></div>
                {canWrite && <span className="actions-cell">
                  <button type="button" className="btn btn-sm" onClick={() => setResource(r)} aria-label={`Edit material ${r.title}`}>Edit</button>
                  <button type="button" className="btn btn-sm btn-danger" onClick={() => setDeleting({ path: `${COURSES_API}/resources/${r.id}`, label: `Material “${r.title}”` })}
                    aria-label={`Remove material ${r.title}`}>Remove</button>
                </span>}
              </div>
            )} />
        )}
        {canWrite && <div><button type="button" className="btn btn-sm" onClick={() => setResource("new")}><IconPlus /> Add study material</button></div>}
      </section>

      {variant && <VariantDialog lectureId={lecture.id} variant={variant === "new" ? null : variant} taken={lecture.variants.map((v) => v.language)} onClose={() => setVariant(null)} />}
      {resource && <ResourceDialog lectureId={lecture.id} resource={resource === "new" ? null : resource} onClose={() => setResource(null)} />}
      <ConfirmDialog open={!!deleting} title={`Remove ${deleting?.label ?? ""}?`} confirmLabel="Remove" destructive busy={remove.isPending}
        onConfirm={confirmDelete} onCancel={() => setDeleting(null)}>
        <p>This can't be undone. The uploaded file stays in storage but is no longer used by this lecture.</p>
      </ConfirmDialog>
    </Dialog>
  );
}

function VariantDialog({ lectureId, variant, taken, onClose }: { lectureId: number; variant: Variant | null; taken: string[]; onClose: () => void }) {
  const toast = useToast();
  const save = useSaveNode<Variant>();
  const [language, setLanguage] = useState(variant?.language ?? Object.keys(LANGUAGES).find((l) => !taken.includes(l)) ?? "hinglish");
  const [source, setSource] = useState<"upload" | "url">(variant?.video_url ? "url" : "upload");
  const [video, setVideo] = useState(ref(variant?.video ?? null));
  const [url, setUrl] = useState(variant?.video_url ?? "");
  const [audio, setAudio] = useState(ref(variant?.audio ?? null));
  const [subtitle, setSubtitle] = useState(ref(variant?.subtitle ?? null));
  const [thumb, setThumb] = useState(ref(variant?.thumbnail ?? null));
  const [description, setDescription] = useState(variant?.description ?? "");
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    const found: Record<string, string> = {};
    if (source === "upload" && !video) found.video_asset_id = "Upload the video for this language.";
    if (source === "url" && !/^https:\/\/\S+$/.test(url.trim())) found.video_url = "Enter an https:// video URL.";
    setErrors(found);
    if (Object.keys(found).length) return;
    const body: VariantBody = {
      language, video_asset_id: source === "upload" ? video?.id ?? null : null, video_url: source === "url" ? url.trim() : null,
      audio_asset_id: audio?.id ?? null, subtitle_asset_id: subtitle?.id ?? null, thumbnail_asset_id: thumb?.id ?? null,
      description: description.trim() || null,
    };
    try {
      await save.mutateAsync({ create: !variant, path: variant ? `${COURSES_API}/variants/${variant.id}` : `${COURSES_API}/lectures/${lectureId}/variants`, body });
      toast.success(`${LANGUAGES[language]} version saved.`);
      onClose();
    } catch (err) {
      setErrors(apiMessage(err, "Could not save this language version."));
    }
  }
  const err = (id: string) => errors[id] && <span className="error" role="alert">{errors[id]}</span>;
  return (
    <Dialog open wide onClose={onClose} title={variant ? `Edit ${variant.language_name} version` : "Add language version"}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="submit" form="variant-form" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
      </>}>
      <form id="variant-form" className="form-grid" onSubmit={submit} noValidate>
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}
        <div className="field">
          <label htmlFor="va-language">Language</label>
          <select id="va-language" value={language} onChange={(e) => setLanguage(e.target.value)} aria-invalid={!!errors.language}>
            {Object.entries(LANGUAGES).map(([code, name]) => (
              <option key={code} value={code} disabled={code !== variant?.language && taken.includes(code)}>{name}{code !== variant?.language && taken.includes(code) ? " (already added)" : ""}</option>
            ))}
          </select>
          {err("language")}
        </div>
        <fieldset className="field">
          <legend>Video source</legend>
          <label className="checkbox"><input type="radio" name="va-source" checked={source === "upload"} onChange={() => setSource("upload")} /> Upload a file</label>
          <label className="checkbox"><input type="radio" name="va-source" checked={source === "url"} onChange={() => setSource("url")} /> Paste a video URL</label>
        </fieldset>
        <div style={{ gridColumn: "1 / -1" }}>
          {source === "upload" ? (
            <FileField kind="video" label="Video file" value={video} onChange={setVideo} error={errors.video_asset_id} hint="MP4, WebM or MOV." />
          ) : (
            <div className="field">
              <label htmlFor="va-url">Video URL</label>
              <input id="va-url" type="url" placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} aria-invalid={!!errors.video_url} />
              {err("video_url")}
            </div>
          )}
        </div>
        <FileField kind="audio" label="Audio file (optional)" value={audio} onChange={setAudio} />
        <FileField kind="subtitle" label="Subtitles (optional)" value={subtitle} onChange={setSubtitle} hint="WebVTT (.vtt) or SubRip (.srt)." />
        <FileField kind="image" label="Thumbnail override (optional)" value={thumb} onChange={setThumb} />
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label htmlFor="va-description">Description (optional)</label>
          <textarea id="va-description" rows={2} maxLength={5000} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
      </form>
    </Dialog>
  );
}

function ResourceDialog({ lectureId, resource, onClose }: { lectureId: number; resource: Resource | null; onClose: () => void }) {
  const toast = useToast();
  const save = useSaveNode<Resource>();
  const [title, setTitle] = useState(resource?.title ?? "");
  const [file, setFile] = useState(ref(resource?.asset ?? null));
  const [downloadable, setDownloadable] = useState(resource?.is_downloadable ?? true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    const found: Record<string, string> = {};
    if (!title.trim()) found.title = "Enter a title.";
    if (!file) found.asset_id = "Upload the file.";
    setErrors(found);
    if (Object.keys(found).length || !file) return;
    const body: ResourceBody = { title: title.trim(), asset_id: file.id, is_downloadable: downloadable };
    try {
      await save.mutateAsync({ create: !resource, path: resource ? `${COURSES_API}/resources/${resource.id}` : `${COURSES_API}/lectures/${lectureId}/resources`, body });
      toast.success("Study material saved.");
      onClose();
    } catch (err) {
      setErrors(apiMessage(err, "Could not save the study material."));
    }
  }
  return (
    <Dialog open onClose={onClose} title={resource ? "Edit study material" : "Add study material"}
      footer={<>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
        <button type="submit" form="resource-form" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
      </>}>
      <form id="resource-form" className="stack" onSubmit={submit} noValidate>
        {errors.form && <Notice tone="critical">{errors.form}</Notice>}
        <div className="field">
          <label htmlFor="re-title">Title</label>
          <input id="re-title" type="text" maxLength={160} value={title} onChange={(e) => setTitle(e.target.value)} aria-invalid={!!errors.title} />
          {errors.title && <span className="error" role="alert">{errors.title}</span>}
        </div>
        <FileField kind="document" label="File (PDF, DOCX, PPTX, TXT)" value={file} onChange={setFile} error={errors.asset_id} />
        <label className="checkbox"><input type="checkbox" checked={downloadable} onChange={(e) => setDownloadable(e.target.checked)} /> Students may download this file</label>
      </form>
    </Dialog>
  );
}
