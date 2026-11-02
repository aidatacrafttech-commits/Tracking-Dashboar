import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/Toast";
import { Notice } from "../../components/ui";
import { AdminApiError } from "./adminApi";
import { useCreateVoucher, usePackages, useSubjects, useUpdateVoucher } from "./hooks";
import {
  CATEGORY_INFO,
  createBody,
  emptyForm,
  FIELD_ALIASES,
  formFromVoucher,
  isoToIstInput,
  PACKAGE_KEYS,
  PACKAGE_LABELS,
  patchBody,
  PREFIX,
  validateForm,
  type VoucherForm as Form,
} from "./shared";
import type { Category, Voucher } from "./types";

const FIELDS = new Set<string>([
  "code", "title", "description", "discount_type", "discount_value", "max_discount_inr", "min_purchase_inr",
  "starts_at", "ends_at", "benefit_validity_days", "per_user_limit", "max_unique_users", "total_limit", "keys",
]);

/** Create (POST) or edit (PATCH, changed fields only) a Course or Live Tutor voucher. */
export function VoucherFormDialog({ category, editing, onClose }: { category: Category; editing: Voucher | null; onClose: () => void }) {
  const toast = useToast();
  const prefix = PREFIX[category];
  const info = CATEGORY_INFO[category];
  const create = useCreateVoucher(prefix);
  const update = useUpdateVoucher(prefix);
  const [form, setForm] = useState<Form>(() => (editing ? formFromVoucher(editing) : emptyForm(isoToIstInput(new Date().toISOString()))));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const busy = create.isPending || update.isPending;
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));
  const codeLocked = !!editing && editing.stats.total_redemptions > 0;
  const percent = form.discount_type === "percent";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const found = validateForm(form, category);
    setErrors(found);
    if (Object.keys(found).length) return;
    try {
      if (editing) {
        const body = patchBody(form, category, editing);
        if (Object.keys(body).length === 0) {
          toast.info("No changes to save.");
          return onClose();
        }
        const { voucher } = await update.mutateAsync({ id: editing.id, body });
        toast.success(`Voucher ${voucher.code} updated.`);
      } else {
        const { voucher } = await create.mutateAsync(createBody(form, category));
        toast.success(`${info.label} voucher ${voucher.code} created.`);
      }
      onClose();
    } catch (err) {
      // Keep the typed values; show the backend's detail text as-is.
      if (err instanceof AdminApiError) {
        const mapped: Record<string, string> = {};
        const other: string[] = [];
        for (const [field, msg] of Object.entries(err.fieldErrors())) {
          const name = FIELD_ALIASES[field] ?? field;
          if (FIELDS.has(name)) mapped[name] = msg;
          else other.push(msg);
        }
        setErrors({ ...mapped, ...(other.length || !Object.keys(mapped).length ? { form: other.join(" ") || err.message } : {}) });
      } else setErrors({ form: "Could not save the voucher." });
    }
  }

  const field = (id: keyof Form & string, label: string, control: ReactNode, hint?: string, wide = false) => (
    <div className="field" style={wide ? { gridColumn: "1 / -1" } : undefined}>
      <label htmlFor={`vf-${id}`}>{label}</label>
      {control}
      {hint && <span className="hint" id={`vf-${id}-hint`}>{hint}</span>}
      {errors[id] && <span className="error" id={`vf-${id}-error`} role="alert">{errors[id]}</span>}
    </div>
  );
  const aria = (id: string) => ({
    "aria-invalid": !!errors[id],
    "aria-describedby": [errors[id] ? `vf-${id}-error` : null, `vf-${id}-hint`].filter(Boolean).join(" "),
  });
  const num = (id: keyof Form & string, label: string, hint: string, min = 1) =>
    field(id, label, <input id={`vf-${id}`} type="number" min={min} step={1} inputMode="numeric" value={form[id] as string}
      onChange={(e) => set(id, e.target.value as never)} {...aria(id)} />, hint);
  const section = (title: string, text?: string) => (
    <h3 className="form-section">{title}{text && <p className="small muted">{text}</p>}</h3>
  );

  return (
    <Dialog open wide onClose={busy ? () => undefined : onClose}
      title={editing ? `Edit ${info.label} voucher ${editing.code}` : `Create ${info.label} voucher`}
      description="Dates are in IST (Asia/Kolkata). Leave optional limits empty for unlimited."
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="voucher-form" className="btn btn-primary" disabled={busy}>
          {busy ? "Saving…" : editing ? "Save changes" : "Create voucher"}
        </button>
      </>}>
      <form id="voucher-form" onSubmit={submit} noValidate className="form-grid" aria-busy={busy}>
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}

        {section("Voucher")}
        {field("code", "Code",
          <input id="vf-code" type="text" maxLength={32} autoComplete="off" value={form.code} disabled={codeLocked}
            onChange={(e) => set("code", e.target.value.toUpperCase())} {...aria("code")} />,
          codeLocked ? "Cannot change after the first redemption." : "Letters A–Z, digits 0–9, - or _. Saved in capitals.")}
        {field("title", "Title (optional)",
          <input id="vf-title" type="text" maxLength={120} value={form.title} onChange={(e) => set("title", e.target.value)} {...aria("title")} />)}
        <div className="field">
          <span className="label">Status</span>
          <label className="checkbox">
            <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            Enabled (can be redeemed while in its date window)
          </label>
        </div>

        {section("Discount")}
        {field("discount_type", "Discount type",
          <select id="vf-discount_type" value={form.discount_type} onChange={(e) => set("discount_type", e.target.value as Form["discount_type"])} {...aria("discount_type")}>
            <option value="percent">Percent (%)</option>
            <option value="fixed">Fixed amount (₹)</option>
          </select>)}
        {field("discount_value", percent ? "Discount (%)" : "Discount (₹)",
          <div className="input-suffix">
            <input id="vf-discount_value" type="number" min={1} max={percent ? 100 : undefined} step={1} inputMode="numeric"
              value={form.discount_value} onChange={(e) => set("discount_value", e.target.value)} {...aria("discount_value")} />
            <span aria-hidden="true">{percent ? "%" : "₹"}</span>
          </div>,
          percent ? "1 to 100." : "Whole rupees, 1 or more.")}
        {percent && num("max_discount_inr", "Maximum discount (₹)", "Optional cap on the rupee discount.")}
        {num("min_purchase_inr", "Minimum purchase (₹)", "Optional. Orders below this cannot use the voucher.", 0)}

        {section(info.scope)}
        <div style={{ gridColumn: "1 / -1" }}>
          {category === "course" ? (
            <SubjectPicker all={form.all} keys={form.keys} error={errors.keys} onChange={(all, keys) => setForm((f) => ({ ...f, all, keys }))} />
          ) : (
            <PackagePicker all={form.all} keys={form.keys} error={errors.keys} onChange={(all, keys) => setForm((f) => ({ ...f, all, keys }))} />
          )}
        </div>

        {section("Validity", "Campaign: when the code can be redeemed (IST). Benefit: days the benefit lasts after redemption.")}
        {field("starts_at", "Starts at (IST)",
          <input id="vf-starts_at" type="datetime-local" value={form.starts_at} onChange={(e) => set("starts_at", e.target.value)} {...aria("starts_at")} />)}
        {field("ends_at", "Ends at (IST)",
          <input id="vf-ends_at" type="datetime-local" value={form.ends_at} onChange={(e) => set("ends_at", e.target.value)} {...aria("ends_at")} />,
          "Optional. Empty = no end date.")}
        {num("benefit_validity_days", "Benefit validity (days)",
          category === "course" ? "Optional, 1–3650: days of course access after redemption." : "Optional, 1–3650: recorded with each redemption.")}

        {section("Limits")}
        {num("per_user_limit", "Uses per user", "How many times one user can redeem it.")}
        {num("max_unique_users", "Maximum unique users", "Optional. Empty = unlimited.")}
        {num("total_limit", "Total redemption limit", "Optional. Empty = unlimited.")}

        {field("description", "Description (optional)",
          <textarea id="vf-description" maxLength={1000} value={form.description} onChange={(e) => set("description", e.target.value)} {...aria("description")} />,
          undefined, true)}
      </form>
    </Dialog>
  );
}

interface PickerProps {
  all: boolean;
  keys: string[];
  error?: string;
  onChange: (all: boolean, keys: string[]) => void;
}

function SubjectPicker({ all, keys, error, onChange }: PickerProps) {
  const { data, isPending, error: loadError } = useSubjects();
  const [q, setQ] = useState("");
  const chosen = new Set(keys);
  const subjects = useMemo(() => {
    const list = (data?.subjects ?? []).filter((s) => s.active || chosen.has(s.key));
    const needle = q.trim().toLowerCase();
    return needle ? list.filter((s) => `${s.name} ${s.key}`.toLowerCase().includes(needle)) : list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, q, keys]);
  const toggle = (key: string, on: boolean) => onChange(false, on ? [...keys, key] : keys.filter((k) => k !== key));
  return (
    <fieldset className="selector" aria-invalid={!!error} aria-describedby={error ? "vf-keys-error" : undefined}>
      <legend>Applicable subjects</legend>
      <label className="checkbox">
        <input type="checkbox" checked={all} onChange={(e) => onChange(e.target.checked, e.target.checked ? [] : keys)} />
        All subjects
      </label>
      {!all && (
        isPending ? <p className="small muted">Loading subjects…</p> : loadError ? <Notice tone="critical">{loadError.message}</Notice> : (
          <>
            <div className="field">
              <label htmlFor="vf-subject-search">Find a subject</label>
              <input id="vf-subject-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name or key" />
            </div>
            <div className="chip-list" role="group" aria-label="Subjects">
              {subjects.map((s) => (
                <label key={s.key} className={`chip ${chosen.has(s.key) ? "on" : ""}`} title={s.key}>
                  <input type="checkbox" checked={chosen.has(s.key)} onChange={(e) => toggle(s.key, e.target.checked)} />
                  {s.name}{!s.active && " (inactive)"}
                </label>
              ))}
              {subjects.length === 0 && <p className="small muted">No subjects match. Add custom subjects on the Subjects tab.</p>}
            </div>
            <span className="hint">{keys.length} selected.</span>
          </>
        )
      )}
      {error && <span className="error" id="vf-keys-error" role="alert">{error}</span>}
    </fieldset>
  );
}

function PackagePicker({ all, keys, error, onChange }: PickerProps) {
  const { data } = usePackages();
  const names = Object.fromEntries((data?.packages ?? []).map((p) => [p.key, p.name]));
  const toggle = (key: string, on: boolean) => onChange(false, PACKAGE_KEYS.filter((k) => (k === key ? on : keys.includes(k))));
  return (
    <fieldset className="selector" aria-invalid={!!error} aria-describedby={error ? "vf-keys-error" : undefined}>
      <legend>Applicable packages</legend>
      <label className="checkbox">
        <input type="checkbox" checked={all} onChange={(e) => onChange(e.target.checked, e.target.checked ? [] : keys)} />
        All packages
      </label>
      <div className="choice-grid" role="group" aria-label="Packages">
        {PACKAGE_KEYS.map((k) => {
          const on = all || keys.includes(k);
          return (
            <label key={k} className={`choice-card ${on ? "on" : ""} ${all ? "disabled" : ""}`}>
              <input type="checkbox" checked={on} disabled={all} onChange={(e) => toggle(k, e.target.checked)} />
              <strong>{PACKAGE_LABELS[k]}</strong>
              <span className="small muted">{names[k] && names[k] !== PACKAGE_LABELS[k] ? names[k] : k}</span>
            </label>
          );
        })}
      </div>
      {error && <span className="error" id="vf-keys-error" role="alert">{error}</span>}
    </fieldset>
  );
}
