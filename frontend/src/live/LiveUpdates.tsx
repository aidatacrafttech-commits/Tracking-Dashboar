import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { buildUrl, notifyUnauthorized } from "../api/client";
import { TOPIC_KEYS } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { config } from "../config";
import { startLiveStream, type LiveState } from "./sse";

const LiveContext = createContext<LiveState>("offline");

export function useLiveState(): LiveState {
  return useContext(LiveContext);
}

function usePageVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

/**
 * Keeps one change-notification stream open while the tab is visible and the user is
 * signed in. Messages only name topics; matching queries are refetched through the API.
 * Polling (refetchInterval) remains as the fallback and stops while the tab is hidden.
 */
export function LiveUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const { status } = useAuth();
  const visible = usePageVisible();
  const [streamState, setState] = useState<LiveState>("connecting");
  const enabled = config.liveUpdates && status === "signed_in";
  const state: LiveState = !enabled ? "offline" : !visible ? "paused" : streamState;

  useEffect(() => {
    if (!enabled || !visible) return;
    // Catch up on anything missed while paused, then listen.
    void queryClient.invalidateQueries();
    return startLiveStream({
      url: buildUrl("/events"),
      onState: setState,
      onUnauthorized: () => notifyUnauthorized("session_expired"),
      onMessage: (message) => {
        if (message.event !== "changed") return;
        try {
          const { topics } = JSON.parse(message.data) as { topics: string[] };
          const prefixes = new Set<string>();
          for (const topic of topics) {
            for (const key of TOPIC_KEYS[topic] ?? []) prefixes.add(JSON.stringify(key));
          }
          for (const key of prefixes) void queryClient.invalidateQueries({ queryKey: JSON.parse(key) });
        } catch {
          /* ignore malformed message */
        }
      },
    });
  }, [enabled, visible, queryClient]);

  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}
