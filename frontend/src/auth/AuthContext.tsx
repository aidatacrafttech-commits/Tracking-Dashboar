import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch, csrfStore, onUnauthorized } from "../api/client";
import type { Role } from "../api/types";

interface SessionResponse {
  role: Role;
  can_write: boolean;
  via: "session" | "bearer";
  csrf_token: string | null;
  expires_at: string | null;
}

interface AuthState {
  status: "checking" | "signed_out" | "signed_in";
  role: Role | null;
  canWrite: boolean;
  /** Why the user was signed out (e.g. an expired session), shown on the sign-in page. */
  notice: string | null;
  signIn: (token: string, remember: boolean) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthState["status"]>("checking");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const clearLocal = useCallback(
    (reason: string | null) => {
      csrfStore.set(null);
      setSession(null);
      setStatus("signed_out");
      setNotice(reason);
      queryClient.clear(); // drop every cached response from the previous session
    },
    [queryClient],
  );

  // Restore an existing session (the HttpOnly cookie) after a reload.
  useEffect(() => {
    const controller = new AbortController();
    apiFetch<SessionResponse>("/auth/session", { signal: controller.signal, quiet401: true })
      .then((info) => {
        csrfStore.set(info.csrf_token);
        setSession(info);
        setStatus("signed_in");
      })
      .catch((err) => {
        if (err instanceof DOMException) return;
        setStatus("signed_out");
        if (err instanceof ApiError && err.status === 0) setNotice("Cannot reach the monitoring server.");
      });
    return () => controller.abort();
  }, []);

  useEffect(
    () =>
      onUnauthorized((code) =>
        clearLocal(code === "session_expired" ? "Your session expired. Please sign in again." : "Please sign in."),
      ),
    [clearLocal],
  );

  const signIn = useCallback(async (token: string, remember: boolean) => {
    const info = await apiFetch<SessionResponse>("/auth/login", {
      method: "POST",
      body: { token, remember },
      quiet401: true,
    });
    csrfStore.set(info.csrf_token);
    setSession(info);
    setNotice(null);
    setStatus("signed_in");
  }, []);

  const signOut = useCallback(async () => {
    try {
      await apiFetch<void>("/auth/logout", { method: "POST", quiet401: true });
    } catch {
      /* already signed out server-side */
    }
    clearLocal("You have signed out.");
  }, [clearLocal]);

  const value = useMemo<AuthState>(
    () => ({ status, role: session?.role ?? null, canWrite: session?.can_write ?? false, notice, signIn, signOut }),
    [status, session, notice, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
