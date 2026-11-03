import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type FormEvent } from "react";
import { ApiError, apiFetch } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { DataTable } from "../components/DataTable";
import { Dialog } from "../components/Dialog";
import { IconRefresh } from "../components/icons";
import { useToast } from "../components/Toast";
import { Badge, EmptyState, ErrorState, LoadingBlock, Notice, PageHeader } from "../components/ui";
import { formatDateTime } from "../lib/format";

// The dashboard's own API: its server adds the AdhyaapakAI admin secret (never the browser).
const KEY = ["account-deletions"] as const;
const IST = "Asia/Kolkata";

interface PendingDeletion {
  user_id: string;
  email: string | null;
  name: string | null;
  requested_at: string | null;
  due_at: string | null;
  days_left: number | null;
}
interface DeletionList {
  count: number;
  grace_days: number;
  deletions: PendingDeletion[];
}
type Action = { kind: "restore" | "purge"; row: PendingDeletion };

const ist = (iso: string | null) => (iso ? formatDateTime(iso, IST) : "—");

function daysLeftBadge(days: number | null) {
  if (days === null) return "—";
  const text = days <= 0 ? "Today" : `${days} day${days === 1 ? "" : "s"}`;
  return days <= 1 ? <Badge tone="critical">{text}</Badge> : <Badge tone={days <= 3 ? "warning" : "info"}>{text}</Badge>;
}

export function AccountDeletionsPage() {
  const { canWrite } = useAuth();
  const [q, setQ] = useState("");
  const [action, setAction] = useState<Action | null>(null);
  // Ask first whether the server has the AdhyaapakAI link, so an unconfigured dashboard shows
  // a setup notice instead of making a call that can only fail.
  const status = useQuery({
    queryKey: [...KEY, "status"],
    queryFn: () => apiFetch<{ configured: boolean; target: string | null }>("/account-deletions/status"),
    staleTime: Infinity,
    retry: false,
  });
  const configured = status.data?.configured === true;
  const list = useQuery({
    queryKey: KEY,
    queryFn: () => apiFetch<DeletionList>("/account-deletions"),
    enabled: configured,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const { data, isFetching, refetch } = list;
  const isPending = status.isPending || (configured && list.isPending);
  const error = status.error ?? list.error;
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.deletions ?? []).filter((d) => !needle || (d.email ?? "").toLowerCase().includes(needle));
  }, [data, q]);

  return (
    <>
      <PageHeader
        title="Account Deletions"
        description={data ? `${data.count} account${data.count === 1 ? "" : "s"} waiting for deletion · ${data.grace_days}-day grace period. Times in IST.`
          : "Accounts in the deletion grace period. Times in IST."}
        actions={
          <button type="button" className="btn" onClick={() => refetch()} disabled={!configured || isFetching}>
            <IconRefresh /> {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        }
      />
      {isPending ? <div className="card"><LoadingBlock rows={5} label="Loading pending deletions" /></div>
        : error ? <LoadError error={error} onRetry={() => (status.error ? status.refetch() : refetch())} />
        : !configured || !data ? (
          <Notice tone="warning">
            <strong>Account deletions are not configured.</strong> Set <code>ADHYAAPAK_API_BASE_URL</code> and{" "}
            <code>ADHYAAPAK_ADMIN_SECRET</code> in the dashboard's <code>backend/.env</code> and restart the dashboard.
          </Notice>
        ) : (
          <div className="stack">
            <Notice tone="info">
              A student who tapped “Delete Account” is signed out and blocked at once, and purged automatically on the date shown.
              Restore only when the request came by email from the registered address. Purged accounts cannot be restored.
            </Notice>
            <section className="card" aria-label="Search">
              <div className="field" style={{ maxWidth: 360 }}>
                <label htmlFor="ad-q">Search by email</label>
                <input id="ad-q" type="search" maxLength={254} value={q} onChange={(e) => setQ(e.target.value)} />
              </div>
            </section>
            <div aria-busy={isFetching}>
              <DataTable
                caption="Accounts waiting for deletion"
                rows={rows}
                rowKey={(d) => d.user_id}
                empty={<div className="card"><EmptyState title={data.deletions.length ? "No account matches this search." : "No accounts are waiting for deletion."} /></div>}
                columns={[
                  { key: "email", header: "Email", render: (d) => d.email ?? "—" },
                  { key: "name", header: "Name", render: (d) => d.name ?? <span className="muted">—</span> },
                  { key: "requested", header: "Requested (IST)", render: (d) => ist(d.requested_at) },
                  { key: "due", header: "Permanent deletion on (IST)", render: (d) => ist(d.due_at) },
                  { key: "days", header: "Days left", render: (d) => daysLeftBadge(d.days_left) },
                  ...(canWrite ? [{
                    key: "actions", header: "Actions", render: (d: PendingDeletion) => (
                      <span className="actions-cell">
                        <button type="button" className="btn btn-sm btn-primary" disabled={!d.email} onClick={() => setAction({ kind: "restore", row: d })}
                          aria-label={`Restore account ${d.email ?? ""}`}>Restore account</button>
                        <button type="button" className="btn btn-sm btn-danger" disabled={!d.email} onClick={() => setAction({ kind: "purge", row: d })}
                          aria-label={`Purge now ${d.email ?? ""}`}>Purge now</button>
                      </span>
                    ),
                  }] : []),
                ]}
              />
              <p className="small muted">{rows.length} shown · refreshes every 60 seconds</p>
            </div>
          </div>
        )}
      {action?.kind === "restore" && <RestoreDialog row={action.row} onClose={() => setAction(null)} />}
      {action?.kind === "purge" && <PurgeDialog row={action.row} onClose={() => setAction(null)} />}
    </>
  );
}

function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.code === "adhyaapak_forbidden")
    return (
      <Notice tone="critical">
        <strong>Not authorised.</strong> The AdhyaapakAI backend rejected the dashboard's admin secret. Check <code>ADHYAAPAK_ADMIN_SECRET</code> in
        the dashboard's <code>backend/.env</code> (it must equal the backend's <code>ADMIN_SECRET</code>) and restart the dashboard.{" "}
        <button type="button" className="btn btn-sm" onClick={onRetry}>Try again</button>
      </Notice>
    );
  if (error instanceof ApiError && error.code === "adhyaapak_not_configured")
    return <Notice tone="warning">{error.message}</Notice>;
  if (error instanceof ApiError && error.status === 403)
    return <Notice tone="critical"><strong>Not authorised.</strong> Your dashboard role can't view account deletions.</Notice>;
  return <ErrorState error={error} onRetry={onRetry} />;
}

/** Remove a row locally after a successful action (the next refresh confirms it). */
function useRemoveRow() {
  const client = useQueryClient();
  return (userId: string) =>
    client.setQueryData<DeletionList>(KEY, (old) =>
      old ? { ...old, count: Math.max(0, old.count - 1), deletions: old.deletions.filter((d) => d.user_id !== userId) } : old);
}

function RestoreDialog({ row, onClose }: { row: PendingDeletion; onClose: () => void }) {
  const toast = useToast();
  const client = useQueryClient();
  const removeRow = useRemoveRow();
  const [verified, setVerified] = useState(false);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const restore = useMutation({
    mutationFn: () => apiFetch<{ status: string; already: boolean }>("/account-deletions/reactivate", { method: "POST", body: { email: row.email, note: note.trim() } }),
  });
  const ready = verified && note.trim().length >= 3;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || restore.isPending) return; // never fire twice
    setProblem(null);
    try {
      const result = await restore.mutateAsync();
      removeRow(row.user_id);
      toast.success(result.already ? "Account was already restored — the student can sign in." : "Account restored — the student can sign in again");
      onClose();
    } catch (err) {
      if (err instanceof ApiError && (err.status === 409 || err.status === 410)) {
        toast.error(err);
        await client.invalidateQueries({ queryKey: KEY });
        onClose();
      } else setProblem(err instanceof ApiError ? err.message : "Could not reach the dashboard server. Try again.");
    }
  }
  return (
    <Dialog open onClose={restore.isPending ? () => undefined : onClose} title="Restore this account?"
      description="Everything returns exactly as it was: courses, progress, tokens and phone. The student gets an email that the account is restored."
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={restore.isPending}>Cancel</button>
        <button type="submit" form="restore-form" className="btn btn-primary" disabled={!ready || restore.isPending}>
          {restore.isPending ? "Restoring…" : "Restore account"}
        </button>
      </>}>
      <form id="restore-form" className="stack" onSubmit={submit} noValidate>
        {problem && <Notice tone="critical">{problem}</Notice>}
        <dl className="kv">
          <dt>Email</dt><dd>{row.email}</dd>
          <dt>Would be deleted on</dt><dd>{ist(row.due_at)}</dd>
        </dl>
        <label className="checkbox">
          <input type="checkbox" checked={verified} onChange={(e) => setVerified(e.target.checked)} />
          I verified this request came from the registered email address
        </label>
        <div className="field">
          <label htmlFor="restore-note">Note (ticket id, who asked)</label>
          <input id="restore-note" type="text" maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} aria-describedby="restore-note-hint" />
          <span className="hint" id="restore-note-hint">Required. This is the only record of why the account was restored.</span>
        </div>
      </form>
    </Dialog>
  );
}

function PurgeDialog({ row, onClose }: { row: PendingDeletion; onClose: () => void }) {
  const toast = useToast();
  const removeRow = useRemoveRow();
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const purge = useMutation({
    mutationFn: () => apiFetch<{ status: string; message?: string | null }>("/account-deletions/purge-now", { method: "POST", body: { email: row.email, note: note.trim() } }),
  });
  const matches = typed.trim().toLowerCase() === (row.email ?? "").toLowerCase();
  const ready = matches && note.trim().length >= 3;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || purge.isPending) return; // never fire twice
    setProblem(null);
    try {
      const result = await purge.mutateAsync();
      removeRow(row.user_id);
      toast.success(result.status === "pending" ? (result.message ?? "Purge already running; it will finish shortly.") : "Account permanently deleted.");
      onClose();
    } catch (err) {
      // e.g. 409 "no pending deletion request": shown as-is; never retried with force.
      setProblem(err instanceof ApiError ? err.message : "Could not reach the dashboard server. Try again.");
    }
  }
  return (
    <Dialog open onClose={purge.isPending ? () => undefined : onClose} title="Permanently delete this account now?"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={purge.isPending}>Cancel</button>
        <button type="submit" form="purge-form" className="btn btn-danger" disabled={!ready || purge.isPending}>
          {purge.isPending ? "Deleting…" : "Purge now"}
        </button>
      </>}>
      <form id="purge-form" className="stack" onSubmit={submit} noValidate>
        <Notice tone="critical">This cannot be undone. All of the student's data is deleted; the email can later register again as a new account.</Notice>
        {problem && <Notice tone="warning">{problem}</Notice>}
        <div className="field">
          <label htmlFor="purge-email">Type the email address to confirm</label>
          <input id="purge-email" type="text" autoComplete="off" spellCheck={false} value={typed} onChange={(e) => setTyped(e.target.value)}
            aria-describedby="purge-email-hint" aria-invalid={typed.length > 0 && !matches} />
          <span className="hint" id="purge-email-hint">{row.email}</span>
        </div>
        <div className="field">
          <label htmlFor="purge-note">Note (why it is deleted now)</label>
          <input id="purge-note" type="text" maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
      </form>
    </Dialog>
  );
}
