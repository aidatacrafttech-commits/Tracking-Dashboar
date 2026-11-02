import { useState, type FormEvent } from "react";
import { DataTable } from "../../components/DataTable";
import { Dialog } from "../../components/Dialog";
import { useToast } from "../../components/Toast";
import { Badge, ErrorState, LoadingBlock, Notice } from "../../components/ui";
import { AdminApiError } from "./adminApi";
import { usePackages, useSavePackage } from "./hooks";
import { rupees } from "./shared";
import type { TutorPackage } from "./types";

/** The three Live Tutor packages: name, price, tokens, status and the linked token pack. */
export function PackagesPanel() {
  const { data, error, isPending, refetch } = usePackages();
  const [editing, setEditing] = useState<TutorPackage | null>(null);
  if (isPending) return <div className="card"><LoadingBlock rows={3} label="Loading packages" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
  return (
    <div className="stack">
      <DataTable
        caption="Live Tutor packages"
        rows={[...data.packages].sort((a, b) => a.sort_order - b.sort_order)}
        rowKey={(p) => p.key}
        columns={[
          { key: "name", header: "Name", render: (p) => p.name },
          { key: "key", header: "Key", render: (p) => <code className="small">{p.key}</code> },
          { key: "price", header: "Price", numeric: true, render: (p) => rupees(p.price_inr) },
          { key: "tokens", header: "Tokens", numeric: true, render: (p) => p.tokens.toLocaleString() },
          { key: "status", header: "Status", render: (p) => (p.active ? <Badge tone="normal">Active</Badge> : <Badge tone="unavailable">Inactive</Badge>) },
          { key: "order", header: "Sort order", numeric: true, render: (p) => p.sort_order },
          { key: "pack", header: "Token pack", render: (p) => (
            p.token_pack_product_id
              ? <span className="small"><code>{p.token_pack_product_id}</code> {p.linked ? <Badge tone="normal">Linked</Badge> : <Badge tone="warning" title="No token pack with this product ID was found">Not found</Badge>}</span>
              : <span className="muted">—</span>
          ) },
          { key: "actions", header: "Actions", render: (p) => <button type="button" className="btn btn-sm" onClick={() => setEditing(p)} aria-label={`Edit ${p.name}`}>Edit</button> },
        ]}
      />
      <p className="small muted">When a token pack is linked, its price and tokens are the ones shown and charged.</p>
      {editing && <PackageDialog pkg={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function PackageDialog({ pkg, onClose }: { pkg: TutorPackage; onClose: () => void }) {
  const toast = useToast();
  const save = useSavePackage();
  const [form, setForm] = useState({
    name: pkg.name,
    price_inr: String(pkg.price_inr),
    tokens: String(pkg.tokens),
    active: pkg.active,
    sort_order: String(pkg.sort_order),
    token_pack_product_id: pkg.token_pack_product_id ?? "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const set = (k: keyof typeof form, v: string | boolean) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (save.isPending) return;
    const found: Record<string, string> = {};
    const name = form.name.trim();
    if (name.length < 2 || name.length > 60) found.name = "2–60 characters.";
    if (!/^\d+$/.test(form.price_inr.trim())) found.price_inr = "Whole rupees, 0 or more.";
    if (!/^\d+$/.test(form.tokens.trim()) || Number(form.tokens) < 1) found.tokens = "A whole number, 1 or more.";
    if (!/^-?\d+$/.test(form.sort_order.trim())) found.sort_order = "A whole number.";
    setErrors(found);
    if (Object.keys(found).length) return;
    try {
      await save.mutateAsync({
        key: pkg.key,
        body: {
          name,
          price_inr: Number(form.price_inr),
          tokens: Number(form.tokens),
          active: form.active,
          sort_order: Number(form.sort_order),
          token_pack_product_id: form.token_pack_product_id.trim() || null,
        },
      });
      toast.success(`${name} saved.`);
      onClose();
    } catch (err) {
      if (err instanceof AdminApiError) {
        const fields = err.fieldErrors();
        setErrors(Object.keys(fields).length ? fields : { form: err.message });
      } else setErrors({ form: "Could not save the package." });
    }
  }

  const input = (id: keyof typeof form, label: string, type = "text", hint?: string) => (
    <div className="field">
      <label htmlFor={`pk-${id}`}>{label}</label>
      <input id={`pk-${id}`} type={type} value={form[id] as string} onChange={(e) => set(id, e.target.value)}
        aria-invalid={!!errors[id]} aria-describedby={errors[id] ? `pk-${id}-error` : undefined} />
      {hint && <span className="hint">{hint}</span>}
      {errors[id] && <span className="error" id={`pk-${id}-error`} role="alert">{errors[id]}</span>}
    </div>
  );
  return (
    <Dialog open onClose={save.isPending ? () => undefined : onClose} title={`Edit package ${pkg.name}`}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={save.isPending}>Cancel</button>
        <button type="submit" form="package-form" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save package"}</button>
      </>}>
      <form id="package-form" className="form-grid" onSubmit={submit} noValidate>
        {errors.form && <div style={{ gridColumn: "1 / -1" }}><Notice tone="critical">{errors.form}</Notice></div>}
        {input("name", "Name")}
        {input("price_inr", "Price (₹)", "number")}
        {input("tokens", "Tokens", "number")}
        {input("sort_order", "Sort order", "number")}
        {input("token_pack_product_id", "Token pack product ID (optional)", "text", "Link a token pack to use its price and tokens. Empty = unlinked.")}
        <div className="field">
          <label className="checkbox">
            <input type="checkbox" checked={form.active} onChange={(e) => set("active", e.target.checked)} />
            Active
          </label>
        </div>
      </form>
    </Dialog>
  );
}
