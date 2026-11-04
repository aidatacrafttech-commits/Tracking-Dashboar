import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { IconLogo } from "../components/icons";

export function LoginPage() {
  const { signIn, notice } = useAuth();
  const [token, setToken] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!token.trim()) {
      setError("Enter your dashboard token.");
      return;
    }
    setBusy(true);
    try {
      await signIn(token.trim(), remember);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "That token was not accepted."
          : err instanceof ApiError && err.status === 429
            ? "Too many failed attempts. Wait 15 minutes and try again."
            : err instanceof ApiError
              ? err.message
              : "Could not reach the server.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="card" onSubmit={submit} noValidate aria-labelledby="login-title">
        <div className="row">
          <IconLogo size={30} />
          <h1 id="login-title">Adhyaapak AI Dashboard</h1>
        </div>
        {notice && <p className="notice tone-info" role="status">{notice}</p>}
        <p className="muted">
          Sign in with the dashboard token configured on the server (<code>DASHBOARD_OWNER_TOKEN</code>, or the
          read-only <code>DASHBOARD_VIEWER_TOKEN</code>).
        </p>
        <div className="field">
          <label htmlFor="token">Dashboard token</label>
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={error ? "token-error" : undefined}
          />
          {error && (
            <span id="token-error" className="error" role="alert">
              {error}
            </span>
          )}
        </div>
        <label className="checkbox">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          Keep me signed in on this device for 7 days
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Checking…" : "Sign in"}
        </button>
        <p className="small muted">
          The token is sent once to this server and is never stored in the browser. You get a session that ends when
          you sign out, after 60 minutes of inactivity, or after 12 hours.
        </p>
      </form>
    </main>
  );
}
