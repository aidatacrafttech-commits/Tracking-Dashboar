import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { useAppSettings } from "../../api/hooks";
import { Notice } from "../../components/ui";
import { useTaxonomy } from "./api";
import { FileField } from "./FileField";
import { emptyCourseForm, formFromCourse, toCourseInput, validateCourse, VALIDITY_PRESETS, type CourseForm as Form } from "./shared";
import type { Course, CourseInput, TaxonomyKind } from "./types";

const FIELDS = new Set(["title", "description", "goals", "thumbnail_asset_id", "class_id", "stream_id", "subject_id", "price",
  "original_price", "discounted_price", "launch_at", "validity_value", "validity_unit", "currency"]);

/** Course details (create and edit). Students' access is counted from each purchase; the
 * launch date only opens the course for purchase, and there is no end date. */
export function CourseForm({
  course, submitLabel, busy, onSubmit, onCancel, readOnly = false, footer,
}: {
  course: Course | null;
  submitLabel: string;
  busy: boolean;
  onSubmit: (input: CourseInput) => Promise<void>;
  onCancel?: () => void;
  readOnly?: boolean;
  footer?: ReactNode;
}) {
  const taxonomy = useTaxonomy();
  const currencies = useAppSettings().data?.supported_currencies ?? ["INR"];
  const [form, setForm] = useState<Form>(() => (course ? formFromCourse(course) : emptyCourseForm()));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const terms = (kind: TaxonomyKind) => (taxonomy.data ?? []).filter((t) => t.kind === kind && (t.is_active || String(t.id) === form[`${kind}_id`]));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || readOnly) return;
    const found = validateCourse(form);
    setErrors(found);
    if (Object.keys(found).length) return;
    try {
      await onSubmit(toCourseInput(form));
    } catch (err) {
      if (err instanceof ApiError) {
        // The API names money fields *_paise; the inputs take rupees.
        const fields = Object.fromEntries(Object.entries(err.fieldErrors()).map(([k, m]) => [k.replace(/_paise$/, ""), m]));
        const known = Object.fromEntries(Object.entries(fields).filter(([k]) => FIELDS.has(k)));
        const other = Object.entries(fields).filter(([k]) => !FIELDS.has(k)).map(([, m]) => m);
        setErrors({ ...known, ...(other.length || !Object.keys(known).length ? { form: other.join(" ") || err.message } : {}) });
      } else setErrors({ form: "Could not save the course. Check your connection and try again." });
    }
  }

  const err = (id: string) => errors[id] && <span className="error" id={`cf-${id}-error`} role="alert">{errors[id]}</span>;
  const aria = (id: string, hint = false) => ({
    id: `cf-${id}`,
    "aria-invalid": !!errors[id],
    "aria-describedby": [errors[id] ? `cf-${id}-error` : null, hint ? `cf-${id}-hint` : null].filter(Boolean).join(" ") || undefined,
    disabled: readOnly,
  });
  const select = (kind: TaxonomyKind, label: string) => (
    <div className="field">
      <label htmlFor={`cf-${kind}_id`}>{label}</label>
      <select value={form[`${kind}_id`]} onChange={(e) => set(`${kind}_id`, e.target.value)} {...aria(`${kind}_id`)}>
        <option value="">{kind === "stream" ? "No stream" : `Choose a ${kind}`}</option>
        {terms(kind).map((t) => <option key={t.id} value={t.id}>{t.name}{t.is_active ? "" : " (inactive)"}</option>)}
      </select>
      {err(`${kind}_id`)}
    </div>
  );
  const money = (id: "price" | "original_price" | "discounted_price", label: string, hint: string) => (
    <div className="field">
      <label htmlFor={`cf-${id}`}>{label}</label>
      <div className="input-suffix">
        <input type="text" inputMode="decimal" value={form[id]} onChange={(e) => set(id, e.target.value)} {...aria(id, true)} />
        <span aria-hidden="true">{form.currency}</span>
      </div>
      <span className="hint" id={`cf-${id}-hint`}>{hint}</span>
      {err(id)}
    </div>
  );

  return (
    <form onSubmit={submit} noValidate className="form-grid" aria-busy={busy} aria-label="Course details">
      {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}

      <h3 className="form-section">Basic information</h3>
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label htmlFor="cf-title">Course name</label>
        <input type="text" maxLength={160} value={form.title} onChange={(e) => set("title", e.target.value)} {...aria("title")} />
        {err("title")}
      </div>
      {select("class", "Class")}
      {select("stream", "Stream (optional)")}
      {select("subject", "Subject")}
      <div style={{ gridColumn: "1 / -1" }}>
        <FileField kind="image" label="Course thumbnail" disabled={readOnly}
          value={form.thumbnail_asset_id ? { id: form.thumbnail_asset_id, name: form.thumbnail_name ?? "thumbnail" } : null}
          onChange={(a) => setForm((f) => ({ ...f, thumbnail_asset_id: a?.id ?? null, thumbnail_name: a?.name ?? null }))}
          error={errors.thumbnail_asset_id} hint="JPG, PNG or WebP." />
      </div>
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label htmlFor="cf-description">Course description</label>
        <textarea rows={4} maxLength={10_000} value={form.description} onChange={(e) => set("description", e.target.value)} {...aria("description")} />
        {err("description")}
      </div>
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label htmlFor="cf-goals">Course goals (optional)</label>
        <textarea rows={3} maxLength={5_000} value={form.goals} onChange={(e) => set("goals", e.target.value)} {...aria("goals")} />
        {err("goals")}
      </div>

      <h3 className="form-section">Pricing</h3>
      <fieldset className="field" style={{ gridColumn: "1 / -1" }} disabled={readOnly}>
        <legend>Course type</legend>
        <div className="row">
          <label className="checkbox"><input type="radio" name="cf-type" checked={!form.is_free} onChange={() => set("is_free", false)} /> Paid</label>
          <label className="checkbox"><input type="radio" name="cf-type" checked={form.is_free} onChange={() => set("is_free", true)} /> Free (no payment needed to enroll)</label>
        </div>
      </fieldset>
      {!form.is_free && (
        <>
          <div className="field">
            <label htmlFor="cf-currency">Currency</label>
            <select value={form.currency} onChange={(e) => set("currency", e.target.value)} {...aria("currency")}>
              {currencies.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          {money("price", "Course price", "The regular price.")}
          {money("discounted_price", "Discounted price (optional)", "If set, students pay this instead.")}
          {money("original_price", "Original price / MRP (optional)", "Shown struck out; at least the course price.")}
          <p className="small muted" style={{ gridColumn: "1 / -1", margin: 0 }}>
            Voucher discounts are applied by the app at checkout; the amount actually paid is recorded on each enrollment.
          </p>
        </>
      )}

      <h3 className="form-section">Launch and validity
        <p className="small muted">The launch date only decides when students can buy. Each student's access starts on their own purchase date. There is no course end date.</p>
      </h3>
      <div className="field">
        <label htmlFor="cf-launch_at">Launch date and time (IST)</label>
        <input type="datetime-local" value={form.launch_at} onChange={(e) => set("launch_at", e.target.value)} {...aria("launch_at", true)} />
        <span className="hint" id="cf-launch_at-hint">Required to publish. A future date shows the course as Scheduled.</span>
        {err("launch_at")}
      </div>
      <div className="field">
        <label htmlFor="cf-validity">Access validity</label>
        <select id="cf-validity" value={form.validity_preset} disabled={readOnly} aria-describedby="cf-validity-hint"
          onChange={(e) => set("validity_preset", e.target.value)}>
          {VALIDITY_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          <option value="custom">Custom duration…</option>
        </select>
        <span className="hint" id="cf-validity-hint">Counted from each student's purchase. Changing it later affects new purchases only.</span>
      </div>
      {form.validity_preset === "custom" && (
        <div className="field">
          <label htmlFor="cf-validity_value">Custom duration</label>
          <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
            <input type="number" min={1} style={{ maxWidth: 110 }} value={form.validity_value} onChange={(e) => set("validity_value", e.target.value)} {...aria("validity_value")} />
            <select aria-label="Duration unit" value={form.validity_unit} disabled={readOnly} onChange={(e) => set("validity_unit", e.target.value as Form["validity_unit"])}>
              <option value="days">days</option>
              <option value="months">months</option>
              <option value="years">years</option>
            </select>
          </div>
          {err("validity_value")}
        </div>
      )}

      {!readOnly && (
        <div className="row" style={{ gridColumn: "1 / -1", justifyContent: "flex-end" }}>
          {footer}
          {onCancel && <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>}
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : submitLabel}</button>
        </div>
      )}
    </form>
  );
}
