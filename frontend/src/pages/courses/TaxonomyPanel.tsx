import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { useToast } from "../../components/Toast";
import { Badge, Card, ErrorState, LoadingBlock } from "../../components/ui";
import { useDeleteTerm, useSaveTerm, useTaxonomy } from "./api";
import type { TaxonomyKind, TaxonomyTerm } from "./types";

const KINDS: { kind: TaxonomyKind; title: string; example: string }[] = [
  { kind: "class", title: "Classes", example: "e.g. Class 12" },
  { kind: "stream", title: "Streams", example: "e.g. Commerce" },
  { kind: "subject", title: "Subjects", example: "e.g. Accountancy" },
];

/** Classes, streams and subjects that courses are filed under. */
export function TaxonomyPanel() {
  const { data, error, isPending, refetch } = useTaxonomy();
  if (isPending) return <div className="card"><LoadingBlock rows={4} label="Loading classes and subjects" /></div>;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
  return (
    <div className="grid grid-3">
      {KINDS.map((k) => <TermList key={k.kind} {...k} terms={data.filter((t) => t.kind === k.kind)} />)}
    </div>
  );
}

function TermList({ kind, title, example, terms }: { kind: TaxonomyKind; title: string; example: string; terms: TaxonomyTerm[] }) {
  const { canWrite } = useAuth();
  const toast = useToast();
  const save = useSaveTerm();
  const remove = useDeleteTerm();
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  async function add(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return setProblem("Enter a name.");
    try {
      await save.mutateAsync({ body: { kind, name: name.trim() } });
      setName("");
      setProblem(null);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Could not add it.");
    }
  }
  const act = (p: Promise<unknown>, done: string) => p.then(() => toast.success(done), (e) => toast.error(e));
  return (
    <Card title={title}>
      {terms.length === 0 ? <p className="small muted">None yet.</p> : (
        <ul className="plain-list">
          {terms.map((t) => (
            <li key={t.id} className="content-row">
              <span>{t.name} {!t.is_active && <Badge tone="unavailable">Inactive</Badge>} <span className="small muted">· {t.course_count} course(s)</span></span>
              {canWrite && (
                <span className="actions-cell">
                  <button type="button" className="btn btn-sm" aria-label={`${t.is_active ? "Deactivate" : "Activate"} ${t.name}`}
                    onClick={() => act(save.mutateAsync({ id: t.id, body: { is_active: !t.is_active } }), `${t.name} ${t.is_active ? "deactivated" : "activated"}.`)}>
                    {t.is_active ? "Deactivate" : "Activate"}
                  </button>
                  <button type="button" className="btn btn-sm btn-danger" disabled={t.course_count > 0} title={t.course_count ? "In use by courses: deactivate instead" : undefined}
                    aria-label={`Delete ${t.name}`} onClick={() => act(remove.mutateAsync(t.id), `${t.name} deleted.`)}>Delete</button>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <form className="row" style={{ alignItems: "flex-end", marginTop: 10 }} onSubmit={add} noValidate>
          <div className="field" style={{ flex: "1 1 140px" }}>
            <label htmlFor={`tx-${kind}`}>Add {kind}</label>
            <input id={`tx-${kind}`} type="text" maxLength={80} placeholder={example} value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!problem} />
          </div>
          <button type="submit" className="btn" disabled={save.isPending}>Add</button>
          {problem && <span className="error" role="alert" style={{ flexBasis: "100%" }}>{problem}</span>}
        </form>
      )}
    </Card>
  );
}
