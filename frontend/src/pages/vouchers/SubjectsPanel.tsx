import { useState, type FormEvent } from "react";
import { DataTable } from "../../components/DataTable";
import { ConfirmDialog, Dialog } from "../../components/Dialog";
import { IconPlus } from "../../components/icons";
import { useToast } from "../../components/Toast";
import { Badge, EmptyState, ErrorState, LoadingBlock, Notice } from "../../components/ui";
import { AdminApiError } from "./adminApi";
import { useCreateSubject, useDeleteSubject, useSubjects, useUpdateSubject } from "./hooks";
import type { Subject } from "./types";

/** Subjects that Course vouchers can target: built-in ones (from the course catalogue) and
 * custom ones added here. Built-in subjects can only be renamed or (de)activated. */
export function SubjectsPanel() {
  const toast = useToast();
  const { data, error, isPending, refetch } = useSubjects();
  const update = useUpdateSubject();
  const remove = useDeleteSubject();
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Subject | "new" | null>(null);
  const [deleting, setDeleting] = useState<Subject | null>(null);
  const needle = q.trim().toLowerCase();
  const rows = (data?.subjects ?? []).filter((s) => !needle || `${s.name} ${s.key} ${s.subject ?? ""}`.toLowerCase().includes(needle));

  async function setActive(s: Subject, active: boolean) {
    try {
      await update.mutateAsync({ key: s.key, body: { active } });
      toast.success(`${s.name} ${active ? "activated" : "deactivated"}.`);
    } catch (e) {
      toast.error(e);
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.key);
      toast.success(`${deleting.name} deleted.`);
    } catch (e) {
      toast.error(e); // 409 when a voucher still uses it: the backend explains
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="stack">
      <section className="card" aria-label="Subject filters">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "1 1 240px" }}>
            <label htmlFor="subj-q">Search</label>
            <input id="subj-q" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name, key or subject" />
          </div>
          <button type="button" className="btn btn-primary" style={{ marginLeft: "auto" }} onClick={() => setEditing("new")}>
            <IconPlus /> Add custom subject
          </button>
        </div>
      </section>
      {isPending ? <div className="card"><LoadingBlock rows={6} label="Loading subjects" /></div> : error ? <ErrorState error={error} onRetry={() => refetch()} /> : (
        <DataTable
          caption="Voucher subjects"
          rows={rows}
          rowKey={(s) => s.key}
          empty={<div className="card"><EmptyState title={needle ? "No subjects match" : "No subjects yet"} /></div>}
          columns={[
            { key: "name", header: "Name", render: (s) => s.name },
            { key: "key", header: "Key", render: (s) => <code className="small">{s.key}</code> },
            { key: "class", header: "Class", render: (s) => s.class_level ?? "—" },
            { key: "subject", header: "Subject", render: (s) => s.subject ?? "—" },
            { key: "products", header: "Products", render: (s) => <span className="small">{s.product_ids.join(", ") || "—"}</span> },
            { key: "type", header: "Type", render: (s) => (s.builtin ? <Badge tone="info">Built-in</Badge> : <Badge tone="normal">Custom</Badge>) },
            { key: "active", header: "Status", render: (s) => (s.active ? <Badge tone="normal">Active</Badge> : <Badge tone="unavailable">Inactive</Badge>) },
            {
              key: "actions",
              header: "Actions",
              render: (s) => (
                <span className="actions-cell">
                  <button type="button" className="btn btn-sm" onClick={() => setEditing(s)} aria-label={`Edit ${s.name}`}>{s.builtin ? "Rename" : "Edit"}</button>
                  <button type="button" className="btn btn-sm" disabled={update.isPending} onClick={() => setActive(s, !s.active)}
                    aria-label={`${s.active ? "Deactivate" : "Activate"} ${s.name}`}>{s.active ? "Deactivate" : "Activate"}</button>
                  {!s.builtin && <button type="button" className="btn btn-sm btn-danger" onClick={() => setDeleting(s)} aria-label={`Delete ${s.name}`}>Delete</button>}
                </span>
              ),
            },
          ]}
        />
      )}
      {editing && <SubjectDialog subject={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog open={!!deleting} title={`Delete subject ${deleting?.name ?? ""}?`} confirmLabel="Delete subject" destructive
        busy={remove.isPending} onConfirm={confirmDelete} onCancel={() => setDeleting(null)}>
        <p>The custom subject <code>{deleting?.key}</code> is removed. A subject that a voucher still uses cannot be deleted; deactivate it instead.</p>
      </ConfirmDialog>
    </div>
  );
}

function SubjectDialog({ subject, onClose }: { subject: Subject | null; onClose: () => void }) {
  const toast = useToast();
  const create = useCreateSubject();
  const update = useUpdateSubject();
  const builtin = !!subject?.builtin;
  const [form, setForm] = useState({
    name: subject?.name ?? "",
    key: "",
    class_level: subject?.class_level?.toString() ?? "",
    subject: subject?.subject ?? "",
    product_ids: subject?.product_ids.join(", ") ?? "",
    active: subject?.active ?? true,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const busy = create.isPending || update.isPending;
  const set = (k: keyof typeof form, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const found: Record<string, string> = {};
    const name = form.name.trim();
    if (name.length < 2 || name.length > 120) found.name = "2–120 characters.";
    if (!subject && form.key.trim() && (form.key.trim().length < 2 || form.key.trim().length > 64)) found.key = "2–64 characters, or empty to generate.";
    const cls = form.class_level.trim();
    if (cls && !(/^\d+$/.test(cls) && +cls >= 1 && +cls <= 12)) found.class_level = "1 to 12, or empty.";
    setErrors(found);
    if (Object.keys(found).length) return;
    const products = form.product_ids.split(",").map((p) => p.trim()).filter(Boolean);
    const body: Record<string, unknown> = builtin
      ? { name, active: form.active }
      : { name, class_level: cls ? Number(cls) : null, subject: form.subject.trim() || null, product_ids: products, active: form.active };
    try {
      if (subject) {
        await update.mutateAsync({ key: subject.key, body });
        toast.success(`Subject ${name} saved.`);
      } else {
        if (form.key.trim()) body.key = form.key.trim();
        if (body.class_level === null) delete body.class_level;
        if (body.subject === null) delete body.subject;
        await create.mutateAsync(body);
        toast.success(`Subject ${name} added.`);
      }
      onClose();
    } catch (err) {
      if (err instanceof AdminApiError) {
        const fields = err.fieldErrors();
        setErrors(Object.keys(fields).length ? fields : { form: err.message });
      } else setErrors({ form: "Could not save the subject." });
    }
  }

  const err = (id: string) => errors[id] && <span className="error" id={`sf-${id}-error`} role="alert">{errors[id]}</span>;
  const aria = (id: string) => ({ "aria-invalid": !!errors[id], "aria-describedby": errors[id] ? `sf-${id}-error` : undefined });
  return (
    <Dialog open onClose={busy ? () => undefined : onClose} title={subject ? `Edit subject ${subject.name}` : "Add custom subject"}
      description={builtin ? "Built-in subjects come from the course catalogue: only the name and status can change." : undefined}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="subject-form" className="btn btn-primary" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </>}>
      <form id="subject-form" className="form-grid" onSubmit={submit} noValidate>
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}
        <div className="field">
          <label htmlFor="sf-name">Name</label>
          <input id="sf-name" type="text" maxLength={120} value={form.name} onChange={(e) => set("name", e.target.value)} {...aria("name")} />
          {err("name")}
        </div>
        {!subject && (
          <div className="field">
            <label htmlFor="sf-key">Key (optional)</label>
            <input id="sf-key" type="text" maxLength={64} value={form.key} onChange={(e) => set("key", e.target.value)} {...aria("key")} />
            <span className="hint">Generated from the name when empty. Cannot change later.</span>
            {err("key")}
          </div>
        )}
        {!builtin && (
          <>
            <div className="field">
              <label htmlFor="sf-class_level">Class (optional)</label>
              <input id="sf-class_level" type="number" min={1} max={12} value={form.class_level} onChange={(e) => set("class_level", e.target.value)} {...aria("class_level")} />
              {err("class_level")}
            </div>
            <div className="field">
              <label htmlFor="sf-subject">Subject (optional)</label>
              <input id="sf-subject" type="text" maxLength={64} value={form.subject} onChange={(e) => set("subject", e.target.value)} {...aria("subject")} />
              {err("subject")}
            </div>
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label htmlFor="sf-product_ids">Product IDs (optional)</label>
              <input id="sf-product_ids" type="text" value={form.product_ids} onChange={(e) => set("product_ids", e.target.value)} {...aria("product_ids")} />
              <span className="hint">Comma-separated products that unlock this subject.</span>
              {err("product_ids")}
            </div>
          </>
        )}
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="checkbox">
            <input type="checkbox" checked={form.active} onChange={(e) => set("active", e.target.checked)} />
            Active (can be chosen for new vouchers)
          </label>
        </div>
      </form>
    </Dialog>
  );
}
