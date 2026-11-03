import { apiFetch } from "../../api/client";

// Read-only counts from the AdhyaapakAI database. The dashboard server holds the database
// URL; the browser gets counts, and per-day signup lists (masked for viewers). Money is in paise.
export const APP_USERS_API = "/app-users";
export const KEY = ["app-users"] as const;
export const RANGES = [7, 30, 90] as const;
export const MAX_RANGE_DAYS = 90;
/** Last N days, or a custom start..end (inclusive calendar days, YYYY-MM-DD). */
export type Selection = { days: (typeof RANGES)[number] } | { start: string; end: string };

export interface AppUsersStatus {
  configured: boolean;
  database: string | null;
}

export interface DayRow {
  date: string; // calendar day in the display timezone, YYYY-MM-DD
  signups: number;
  deleted_since: number;
  purchases: number;
  vouchers: number;
  revenue_paise: number;
}

export interface Breakdown {
  label: string;
  count: number;
}

export interface AppUsersSummary {
  timezone: string;
  start: string;
  end: string;
  days: number;
  generated_at: string;
  active_tracked_since: string;
  today: {
    date: string;
    signups: number;
    signups_yesterday: number;
    active_today: number;
    active_7d: number;
    active_30d: number;
    purchases: number;
    vouchers: number;
    revenue_paise: number;
    deletions_scheduled: number;
  };
  totals: {
    signups: number;
    deleted_since: number;
    average_per_day: number;
    best_day: string | null;
    best_day_signups: number;
    with_course: number;
    purchases: number;
    vouchers: number;
    revenue_paise: number;
    other_currency_purchases: number;
    total_users: number;
  };
  daily: DayRow[];
  by_class: Breakdown[];
  by_stream: Breakdown[];
  by_goal: Breakdown[];
}

export interface SignupRow {
  signed_up_at: string;
  name: string | null; // full name for the owner, first name only when masked
  class_level: number | null;
  stream: string | null;
  goals: string[];
  email: string | null; // masked by the server for viewers
  phone: string | null; // masked by the server for viewers
  has_course: boolean;
  deletion_scheduled: boolean;
}

export interface SignupList {
  start: string; // inclusive calendar days, YYYY-MM-DD
  end: string;
  timezone: string;
  masked: boolean; // viewers get masked contact details; the owner sees them in full
  more: boolean;
  signups: SignupRow[];
}

export const fetchStatus = () => apiFetch<AppUsersStatus>(`${APP_USERS_API}/status`);
export const fetchSummary = (sel: Selection) =>
  apiFetch<AppUsersSummary>(`${APP_USERS_API}/summary?${"days" in sel ? `days=${sel.days}` : `start=${sel.start}&end=${sel.end}`}`);
export const fetchSignups = (start: string, end: string) =>
  apiFetch<SignupList>(`${APP_USERS_API}/signups?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
export interface DeletionItem {
  requested_at: string | null;
  status: string | null; // scheduled, reactivated, in_progress or completed
  due_at: string | null;
  completed_at: string | null;
  reactivated_at: string | null;
  signed_up_at: string | null;
  profile_exists: boolean; // false once permanently deleted (the app removes the profile)
  name: string | null;
  email: string | null; // masked by the server for viewers
  phone: string | null; // masked by the server for viewers
}

export interface DeletionList {
  start: string;
  end: string;
  timezone: string;
  masked: boolean;
  more: boolean;
  deletions: DeletionItem[];
}

export const fetchDeletions = (start: string, end: string) =>
  apiFetch<DeletionList>(`${APP_USERS_API}/deletions?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
export interface LiveNow {
  online_now: number; // distinct users in any signal below
  app_open: number | null; // app heartbeats; null until the app sends them
  using_app: number; // any app request in the last `online_window_minutes`
  watching_video: number; // last 25 seconds
  in_live_tutor: number; // last 30 seconds
  asking_doubts: number; // last 5 minutes
  taking_test: number; // last 5 minutes
  online_window_minutes: number;
  checked_at: string;
}

export const fetchLive = () => apiFetch<LiveNow>(`${APP_USERS_API}/live`);

/** Today's calendar date (YYYY-MM-DD) in a timezone. */
export const todayIn = (timeZone: string) => new Intl.DateTimeFormat("en-CA", { timeZone }).format(new Date());
