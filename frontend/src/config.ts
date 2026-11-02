// Build-time UI settings (see .env.example). Nothing here is secret.

function positiveNumber(raw: string | undefined, fallback: number, min: number): number {
  const value = Number(raw);
  return Number.isFinite(value) && value >= min ? value : fallback;
}

export const config = {
  /** Background refetch interval while the tab is visible. */
  pollIntervalMs: positiveNumber(import.meta.env.VITE_POLL_INTERVAL_SECONDS, 60, 10) * 1000,
  /** Server-Sent Events channel for change notifications. */
  liveUpdates: import.meta.env.VITE_LIVE_UPDATES !== "false",
};
