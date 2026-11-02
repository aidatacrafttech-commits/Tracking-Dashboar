import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import { IconClose } from "./icons";

type ToastTone = "normal" | "critical" | "info";
interface ToastItem {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastApi {
  success: (message: string) => void;
  error: (error: unknown, fallback?: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);
let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const dismiss = useCallback((id: number) => setItems((all) => all.filter((t) => t.id !== id)), []);
  const push = useCallback(
    (tone: ToastTone, message: string) => {
      const id = nextId++;
      setItems((all) => [...all.slice(-3), { id, tone, message }]);
      window.setTimeout(() => dismiss(id), tone === "critical" ? 9000 : 5000);
    },
    [dismiss],
  );
  const api = useMemo<ToastApi>(
    () => ({
      success: (m) => push("normal", m),
      info: (m) => push("info", m),
      error: (err, fallback = "The request failed.") =>
        push("critical", err instanceof ApiError ? `${err.message}${err.requestId ? ` (request ${err.requestId})` : ""}` : fallback),
    }),
    [push],
  );
  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="toast" data-tone={t.tone}>
            <span>{t.message}</span>
            <button type="button" className="btn btn-ghost btn-sm icon-btn" onClick={() => dismiss(t.id)} aria-label="Dismiss notification">
              <IconClose size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
