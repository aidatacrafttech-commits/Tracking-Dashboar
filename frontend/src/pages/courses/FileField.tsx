import { createContext, useContext, useId, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { completeUpload, requestUpload, useStorageStatus } from "./api";
import type { AssetKind, AssetRef } from "./types";

const ACCEPT: Record<AssetKind, string> = {
  image: ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp",
  video: ".mp4,.m4v,.webm,.mov,video/mp4,video/webm,video/quicktime",
  audio: ".mp3,.m4a,.aac,.ogg,.wav",
  subtitle: ".vtt,.srt",
  document: ".pdf,.docx,.pptx,.txt",
};
const FALLBACK_TYPES: Record<string, string> = { ".vtt": "text/vtt", ".srt": "application/x-subrip", ".m4a": "audio/mp4" };

/** The course being edited: its files are stored under {prefix}{course_id}/ (the AdhyaapakAI
 * layout). Outside a course (a new course's thumbnail) files go under {prefix}shared/. */
export const CourseScope = createContext<number | undefined>(undefined);

/** Browser -> S3 (presigned POST, so the file never passes through this server), then the
 * server verifies the stored object before the file can be used. */
export function uploadToStorage(kind: AssetKind, file: File, onProgress: (pct: number) => void, courseId?: number): Promise<AssetRef> {
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  const contentType = file.type || FALLBACK_TYPES[ext] || "application/octet-stream";
  return requestUpload({ kind, filename: file.name, content_type: contentType, size_bytes: file.size, course_id: courseId }).then(
    (ticket) =>
      new Promise<AssetRef>((resolve, reject) => {
        const form = new FormData();
        for (const [k, v] of Object.entries(ticket.fields)) form.append(k, v);
        form.append("file", file);
        const xhr = new XMLHttpRequest();
        xhr.open("POST", ticket.upload_url);
        xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
        xhr.onerror = () => reject(new Error("The upload to storage failed. Check the bucket's CORS settings and your connection."));
        xhr.onload = () =>
          xhr.status >= 200 && xhr.status < 300
            ? completeUpload(ticket.asset.id).then(resolve, reject)
            : reject(new Error(`Storage refused the upload (${xhr.status}). The file may be too large or the wrong type.`));
        xhr.send(form);
      }),
  );
}

/** A labelled file picker that uploads immediately and reports the stored asset. */
export function FileField({
  kind, label, value, onChange, error, hint, disabled,
}: {
  kind: AssetKind;
  label: string;
  value: { id: number; name: string } | null;
  onChange: (asset: { id: number; name: string } | null) => void;
  error?: string;
  hint?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const storage = useStorageStatus();
  const courseId = useContext(CourseScope);
  const [progress, setProgress] = useState<number | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const configured = storage.data?.configured ?? false;

  async function pick(file: File | undefined) {
    if (!file) return;
    setProblem(null);
    setProgress(0);
    try {
      const asset = await uploadToStorage(kind, file, setProgress, courseId);
      onChange({ id: asset.id, name: asset.filename });
    } catch (err) {
      setProblem(err instanceof ApiError ? (Object.values(err.fieldErrors())[0] ?? err.message) : err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setProgress(null);
      if (input.current) input.current.value = "";
    }
  }

  const shown = problem ?? error;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {value && (
        <span className="row small" style={{ gap: 8 }}>
          <span>📎 {value.name}</span>
          {!disabled && <button type="button" className="btn btn-sm" onClick={() => onChange(null)} aria-label={`Remove ${label}`}>Remove</button>}
        </span>
      )}
      <input ref={input} id={id} type="file" accept={ACCEPT[kind]} disabled={disabled || !configured || progress !== null}
        onChange={(e) => pick(e.target.files?.[0])} aria-invalid={!!shown} aria-describedby={`${id}-hint`} />
      {progress !== null && <progress max={100} value={progress} aria-label={`Uploading ${label}`}>{progress}%</progress>}
      <span className="hint" id={`${id}-hint`}>
        {!storage.isPending && !configured ? "Uploads are off: set COURSE_ASSETS_BUCKET on the server." : hint}
      </span>
      {shown && <span className="error" role="alert">{shown}</span>}
    </div>
  );
}
