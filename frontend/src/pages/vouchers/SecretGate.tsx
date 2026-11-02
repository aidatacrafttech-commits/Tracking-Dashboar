import { useState, type FormEvent, type ReactNode } from "react";
import { ADHYAAPAK_API_BASE, adminFetch, adminSecret, AdminApiError, useAdminSecret } from "./adminApi";
import { Notice } from "../../components/ui";

/** Shows the voucher admin only once a working admin secret has been entered. */
export function SecretGate({ children }: { children: ReactNode }) {
  const { secret, rejected } = useAdminSecret();
  if (!ADHYAAPAK_API_BASE)
    return (
      <Notice tone="critical">
        The AdhyaapakAI API is not configured. Set <code>VITE_ADHYAAPAK_API_BASE</code> in <code>frontend/.env</code> (see{" "}
        <code>.env.example</code>) and restart the dev server.
      </Notice>
    );
  return secret ? <>{children}</> : <SecretForm rejected={rejected} />;
}

function SecretForm({ rejected }: { rejected: boolean }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const candidate = value.trim();
    if (!candidate) return setError("Enter the admin secret.");
    setBusy(true);
    setError(null);
    try {
      // A cheap admin call proves the secret before anything else is loaded.
      await adminFetch("/vouchers/subjects", { secret: candidate });
      setValue("");
      adminSecret.set(candidate);
    } catch (err) {
      setError(err instanceof AdminApiError && err.status === 403 ? "That admin secret was not accepted." : err instanceof Error ? err.message : "Could not check the secret.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="secret-title" style={{ maxWidth: 520 }}>
      <form onSubmit={submit} className="stack" noValidate>
        <h2 id="secret-title">Unlock Voucher Management</h2>
        <p className="muted">
          Voucher Management uses the AdhyaapakAI backend ({ADHYAAPAK_API_BASE}). Enter its admin secret to continue. The secret is kept
          only in this browser tab and is cleared when you sign out or close the tab.
        </p>
        {rejected && !error && <Notice tone="warning">The admin secret was not accepted. Enter it again.</Notice>}
        <div className="field">
          <label htmlFor="admin-secret">Admin secret</label>
          <input id="admin-secret" type="password" autoComplete="off" spellCheck={false} value={value}
            onChange={(e) => setValue(e.target.value)} aria-invalid={!!error} aria-describedby={error ? "admin-secret-error" : undefined} />
          {error && <span className="error" id="admin-secret-error" role="alert">{error}</span>}
        </div>
        <div>
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Checking…" : "Continue"}</button>
        </div>
      </form>
    </section>
  );
}
