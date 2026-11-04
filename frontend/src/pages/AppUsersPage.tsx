import { useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { DataTable } from "../components/DataTable";
import { IconRefresh } from "../components/icons";
import { Badge, Card, EmptyState, ErrorState, LoadingBlock, Notice, PageHeader, Skeleton, StatCard } from "../components/ui";
import { formatDate, formatDateTime } from "../lib/format";
import { money } from "./courses/shared";
import { DailyBars } from "./appUsers/DailyBars";
import {
  fetchDeletions, fetchLive, fetchSignups, fetchStatus, fetchSummary, KEY, MAX_RANGE_DAYS, RANGES, todayIn,
  type AppUsersSummary, type Breakdown, type Selection,
} from "./appUsers/api";

const IST = "Asia/Kolkata";
const n = (v: number) => v.toLocaleString("en-IN");
const day = (date: string | null, tz: string) => (date ? formatDate(`${date}T12:00:00Z`, tz) : "—");
const spanDays = (start: string, end: string) => Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
const LIVE = { refetchInterval: 60_000, refetchIntervalInBackground: false, retry: false } as const;

/** Problem with a custom range, or null when it can be asked for. */
function rangeProblem(start: string, end: string, today: string): string | null {
  if (!start || !end) return "Choose both dates.";
  if (start > end) return "The start date is after the end date.";
  if (end > today) return "The end date is in the future.";
  if (spanDays(start, end) > MAX_RANGE_DAYS) return `Choose at most ${MAX_RANGE_DAYS} days.`;
  return null;
}

export function AppUsersPage() {
  const today = todayIn(IST);
  const [preset, setPreset] = useState<string>("30");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState(today);
  const [list, setList] = useState({ start: today, end: today });
  const listRef = useRef<HTMLDivElement>(null);
  // Ask first whether the server has the database link, so an unconfigured dashboard shows
  // a setup notice instead of making calls that can only fail.
  const status = useQuery({ queryKey: [...KEY, "status"], queryFn: fetchStatus, staleTime: Infinity, retry: false });
  const configured = status.data?.configured === true;

  const custom = preset === "custom";
  const problem = custom ? rangeProblem(from, to, today) : null;
  const selection: Selection | null = custom ? (problem ? null : { start: from, end: to }) : { days: Number(preset) as (typeof RANGES)[number] };
  const summary = useQuery({
    queryKey: [...KEY, "summary", selection],
    queryFn: () => fetchSummary(selection as Selection),
    enabled: configured && selection !== null,
    placeholderData: (prev) => prev,
    ...LIVE,
  });
  const client = useQueryClient();
  const isFetching = useIsFetching({ queryKey: KEY }) > 0;

  function showDay(date: string) {
    setList({ start: date, end: date });
    listRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }

  return (
    <>
      <PageHeader
        title="App Users"
        description="New signups and activity in the AdhyaapakAI app, by calendar day in IST. Refreshes every 60 seconds."
        actions={
          <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
            <div className="field" style={{ margin: 0 }}>
              <label htmlFor="au-range" className="visually-hidden">Range</label>
              <select id="au-range" value={preset} onChange={(e) => setPreset(e.target.value)} disabled={!configured}>
                {RANGES.map((r) => <option key={r} value={r}>Last {r} days</option>)}
                <option value="custom">Custom dates…</option>
              </select>
            </div>
            {custom && (
              <>
                <div className="field" style={{ margin: 0 }}>
                  <label htmlFor="au-from">From</label>
                  <input id="au-from" type="date" value={from} max={to || today} onChange={(e) => setFrom(e.target.value)} />
                </div>
                <div className="field" style={{ margin: 0 }}>
                  <label htmlFor="au-to">To</label>
                  <input id="au-to" type="date" value={to} min={from || undefined} max={today} onChange={(e) => setTo(e.target.value)} />
                </div>
              </>
            )}
            <button type="button" className="btn" disabled={!configured || isFetching}
              onClick={() => void client.invalidateQueries({ queryKey: KEY })}>
              <IconRefresh /> {isFetching ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        }
      />
      {status.isPending ? <div className="card"><LoadingBlock rows={5} label="Loading app users" /></div>
        : status.error ? <ErrorState error={status.error} onRetry={() => status.refetch()} />
        : !configured ? (
          <Notice tone="warning">
            <strong>App users are not configured.</strong> Set <code>ADHYAAPAK_MONGO_URL</code> and <code>ADHYAAPAK_MONGO_DB</code> in the
            dashboard's <code>backend/.env</code> and restart the dashboard.
          </Notice>
        ) : summary.error ? <LoadError error={summary.error} onRetry={() => summary.refetch()} />
        : (
          <div className="stack">
            {custom && problem && from && <Notice tone="warning">{problem}</Notice>}
            <TodaySection data={summary.data} enabled={configured} />
            <div ref={listRef}>
              <PeopleList range={list} today={today} onRange={setList} enabled={configured} />
            </div>
            {custom && !selection ? <Notice tone="info">Choose a start and end date (up to {MAX_RANGE_DAYS} days) to see that period.</Notice>
              : <PeriodSection data={summary.data} onSelectDay={showDay} />}
            <p className="small muted">
              A “signup” is a completed registration in the app. The app records no install event, so install counts are only in
              Play Console and App Store Connect.
            </p>
          </div>
        )}
    </>
  );
}

function LoadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (error instanceof ApiError && error.code === "app_users_forbidden")
    return (
      <Notice tone="critical">
        <strong>Not authorised.</strong> The app database refused the dashboard's login. Check <code>ADHYAAPAK_MONGO_URL</code> in the
        dashboard's <code>backend/.env</code> and restart the dashboard.
      </Notice>
    );
  return <ErrorState error={error} onRetry={onRetry} />;
}

/** Who is using the app right now: every live signal the app writes (any request, video
 * progress, Live Tutor, doubts, tests, and app heartbeats once enabled), each person once.
 * Refreshes every 3 s. */
function OnlineNow({ enabled, activeToday }: { enabled: boolean; activeToday: number }) {
  const { data, error } = useQuery({ queryKey: [...KEY, "live"], queryFn: fetchLive, enabled, refetchInterval: 3_000, refetchIntervalInBackground: false, retry: false });
  const parts = data ? [
    `${n(data.watching_video)} watching a video`,
    `${n(data.in_live_tutor)} in Live Tutor`,
    data.asking_doubts ? `${n(data.asking_doubts)} asking doubts` : null,
    data.taking_test ? `${n(data.taking_test)} took a test` : null,
    `${n(activeToday)} active today`,
  ].filter(Boolean) : [];
  return (
    <StatCard
      label="Online now"
      value={<span className="row" style={{ gap: 8, alignItems: "center" }}>
        <span className="live-dot" data-state={data ? "live" : "connecting"} aria-hidden="true" />
        {data ? n(data.online_now) : error ? "—" : "…"}
      </span>}
      sub={parts.join(" · ")}
    />
  );
}

function TodaySection({ data, enabled }: { data: AppUsersSummary | undefined; enabled: boolean }) {
  const t = data?.today;
  const tz = data?.timezone ?? IST;
  const diff = t ? t.signups - t.signups_yesterday : 0;
  const trend = diff > 0 ? ` · ${n(diff)} more today` : diff < 0 ? ` · ${n(-diff)} fewer today` : "";
  return (
    <Card title={t ? `Today · ${day(t.date, tz)}` : "Today"} description="Since midnight IST.">
      <div className="grid grid-stats" aria-busy={!t}>
        {!t ? Array.from({ length: 5 }, (_, i) => <div key={i} className="card"><Skeleton height={44} /></div>) : (
          <>
            <StatCard label="New signups today" value={n(t.signups)} sub={`Yesterday ${n(t.signups_yesterday)}${trend}`} />
            <OnlineNow enabled={enabled} activeToday={t.active_today} />
            <StatCard label="Course purchases today" value={n(t.purchases)} sub={`${money(t.revenue_paise, "INR")}${t.vouchers ? ` · ${n(t.vouchers)} by voucher` : ""}`} />
            <StatCard label="Total users" value={n(data.totals.total_users)} sub="All accounts in the app" />
            <StatCard label="Deletions scheduled" value={n(t.deletions_scheduled)} sub="In the 7-day grace period"
              tone={t.deletions_scheduled ? "warning" : undefined} />
          </>
        )}
      </div>
      {data && (
        <p className="small muted" style={{ marginTop: 8 }}>
          Online now counts everyone using the app: anyone who opened it or did anything in the last 12 minutes, and people watching a video, in Live Tutor or asking doubts right now. Refreshes every 3 seconds. Activity is tracked since {day(data.active_tracked_since, tz)}.
        </p>
      )}
    </Card>
  );
}

type DateRange = { start: string; end: string };
type ListKind = "signups" | "deletions";

const DELETION_STATUS: Record<string, { label: string; tone: "warning" | "normal" | "critical" | "info" }> = {
  scheduled: { label: "Scheduled", tone: "warning" },
  in_progress: { label: "Deleting", tone: "critical" },
  completed: { label: "Deleted", tone: "critical" },
  reactivated: { label: "Restored", tone: "normal" },
};

/** One list with a choice first: new users (signups) or deletion requests, then dates and search. */
function PeopleList({ range, today, onRange, enabled }: { range: DateRange; today: string; onRange: (r: DateRange) => void; enabled: boolean }) {
  const [kind, setKind] = useState<ListKind>("signups");
  const [q, setQ] = useState("");
  const problem = rangeProblem(range.start, range.end, today);
  const ready = enabled && !problem;
  const signups = useQuery({
    queryKey: [...KEY, "signups", range.start, range.end],
    queryFn: () => fetchSignups(range.start, range.end),
    enabled: ready && kind === "signups",
    ...LIVE,
  });
  const deletions = useQuery({
    queryKey: [...KEY, "deletions", range.start, range.end],
    queryFn: () => fetchDeletions(range.start, range.end),
    enabled: ready && kind === "deletions",
    ...LIVE,
  });
  const active = kind === "signups" ? signups : deletions;
  const masked = active.data?.masked;
  const tz = active.data?.timezone ?? IST;
  const needle = q.trim().toLowerCase();
  const matches = (r: { name: string | null; email: string | null; phone: string | null }) =>
    !needle || [r.name, r.email, r.phone].some((v) => (v ?? "").toLowerCase().includes(needle));
  const signupRows = useMemo(() => (signups.data?.signups ?? []).filter(matches), [signups.data, needle]); // eslint-disable-line react-hooks/exhaustive-deps
  const deletionRows = useMemo(() => (deletions.data?.deletions ?? []).filter(matches), [deletions.data, needle]); // eslint-disable-line react-hooks/exhaustive-deps

  const single = range.start === range.end;
  const isToday = single && range.start === today;
  const noun = kind === "signups" ? "signups" : "deletion requests";
  const Noun = kind === "signups" ? "Signups" : "Deletion requests";
  const title = isToday ? `Today's ${noun}`
    : single ? `${Noun} on ${day(range.start, IST)}`
    : `${Noun} from ${day(range.start, IST)} to ${day(range.end, IST)}`;
  const total = kind === "signups" ? signups.data?.signups.length : deletions.data?.deletions.length;
  const shown = kind === "signups" ? signupRows.length : deletionRows.length;
  // Right-hand count for the current list: "128 new users", or "12 of 128
  // shown" while a search narrows it. A "+" marks a list the server cut off.
  const countNoun = kind === "signups" ? "new users" : "deletion requests";
  const totalLabel = total === undefined ? "" : `${n(total)}${active.data?.more ? "+" : ""}`;
  const countText = total === undefined ? ""
    : needle && shown !== total ? `${n(shown)} of ${totalLabel} shown`
    : `${totalLabel} ${total === 1 ? countNoun.replace(/s$/, "") : countNoun}`;
  const emptyTitle = total ? "No one matches this search."
    : kind === "signups" ? (isToday ? "No one has signed up yet today." : single ? "No one signed up on this day." : "No one signed up in these dates.")
    : (isToday ? "No deletion requests today." : single ? "No deletion requests on this day." : "No deletion requests in these dates.");

  return (
    <Card title={title}
      description={masked ? "Newest first. Email and phone are masked: only the owner sees full details."
        : "Newest first. Name, email and phone are the student's contact details: don't copy or share them outside the team."}>
      {/* Same dropdown style as the "Last N days" range picker above. */}
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="field" style={{ margin: 0, minWidth: 160 }}>
          <label htmlFor="au-list-kind" className="visually-hidden">Show</label>
          <select id="au-list-kind" value={kind} onChange={(e) => setKind(e.target.value as ListKind)}>
            <option value="signups">New users</option>
            <option value="deletions">Deletions</option>
          </select>
        </div>
        {countText && (
          <div className="small muted" style={{ marginLeft: "auto", fontWeight: 600 }} aria-live="polite" data-testid="people-count">
            {countText}
          </div>
        )}
      </div>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 }}>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="au-list-from">From date</label>
          <input id="au-list-from" type="date" value={range.start} max={range.end || today}
            onChange={(e) => onRange({ ...range, start: e.target.value })} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="au-list-to">To date</label>
          <input id="au-list-to" type="date" value={range.end} min={range.start || undefined} max={today}
            onChange={(e) => onRange({ ...range, end: e.target.value })} />
        </div>
        {!isToday && <button type="button" className="btn btn-sm" onClick={() => onRange({ start: today, end: today })}>Today</button>}
        <div className="field" style={{ margin: 0, minWidth: 220 }}>
          <label htmlFor="au-q">Search name, email or phone</label>
          <input id="au-q" type="search" maxLength={100} value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      {problem ? <Notice tone="warning">{problem}</Notice>
        : active.isPending ? <LoadingBlock rows={4} label={`Loading ${noun}`} />
        : active.error ? <ErrorState error={active.error} onRetry={() => void active.refetch()} />
        : kind === "signups" ? (
          <DataTable
            caption={title}
            rows={signupRows}
            rowKey={(r) => `${r.signed_up_at}-${r.email ?? ""}-${r.phone ?? ""}`}
            empty={<EmptyState title={emptyTitle} />}
            columns={[
              { key: "time", header: "Signed up (IST)", render: (r) => formatDateTime(r.signed_up_at, tz) },
              { key: "name", header: "Name", render: (r) => r.name ?? <span className="muted">—</span> },
              { key: "phone", header: "Phone", render: (r) => r.phone ?? "—" },
              { key: "email", header: "Email", render: (r) => r.email ?? "—" },
              { key: "class", header: "Class", render: (r) => (r.class_level ? `Class ${r.class_level}` : "—") },
              { key: "stream", header: "Stream", render: (r) => r.stream ?? "—" },
              { key: "goals", header: "Goals", render: (r) => (r.goals.length ? r.goals.join(", ") : "—") },
              {
                key: "status", header: "Status", render: (r) => (
                  <span className="row">
                    {r.has_course && <Badge tone="normal">Has course</Badge>}
                    {r.deletion_scheduled && <Badge tone="warning">Deletion scheduled</Badge>}
                    {!r.has_course && !r.deletion_scheduled && <span className="muted">—</span>}
                  </span>
                ),
              },
            ]}
          />
        ) : (
          <DataTable
            caption={title}
            rows={deletionRows}
            rowKey={(r) => `${r.requested_at ?? ""}-${r.signed_up_at ?? ""}-${r.status ?? ""}`}
            empty={<EmptyState title={emptyTitle} />}
            columns={[
              { key: "requested", header: "Requested (IST)", render: (r) => (r.requested_at ? formatDateTime(r.requested_at, tz) : "—") },
              { key: "name", header: "Name", render: (r) => (r.profile_exists ? (r.name ?? "—") : <span className="muted">Details removed</span>) },
              { key: "phone", header: "Phone", render: (r) => r.phone ?? "—" },
              { key: "email", header: "Email", render: (r) => r.email ?? "—" },
              {
                key: "status", header: "Status", render: (r) => {
                  const s = DELETION_STATUS[r.status ?? ""];
                  return s ? <Badge tone={s.tone}>{s.label}</Badge> : <span className="muted">{r.status ?? "—"}</span>;
                },
              },
              {
                key: "when", header: "Deleted on / due (IST)", render: (r) =>
                  r.completed_at ? formatDateTime(r.completed_at, tz)
                    : r.status === "reactivated" ? <span className="muted">Restored{r.reactivated_at ? ` ${formatDateTime(r.reactivated_at, tz)}` : ""}</span>
                    : r.due_at ? `Due ${formatDateTime(r.due_at, tz)}` : "—",
              },
              { key: "joined", header: "Signed up", render: (r) => (r.signed_up_at ? formatDate(r.signed_up_at, tz) : "—") },
            ]}
          />
        )}
      {!problem && !active.isPending && !active.error && (
        <p className="small muted">
          {shown} shown{active.data?.more ? " · only the latest are listed" : ""}.{" "}
          {kind === "signups" ? "Accounts deleted since are not listed."
            : "A permanently deleted account keeps no name, email or phone: the app removes them."}
        </p>
      )}
    </Card>
  );
}

function PeriodSection({ data, onSelectDay }: { data: AppUsersSummary | undefined; onSelectDay: (date: string) => void }) {
  if (!data) return <div className="card"><LoadingBlock rows={6} label="Loading daily signups" /></div>;
  const t = data.totals;
  const tz = data.timezone;
  const pct = (part: number, whole: number) => (whole ? `${Math.round((part / whole) * 100)}%` : "—");
  const remaining = t.signups - t.deleted_since;
  const title = `${day(data.start, tz)} – ${day(data.end, tz)} (${data.days} days)`;
  return (
    <Card title={title} description="Signups per calendar day (IST). Accounts deleted since still count on the day they signed up. Click a day to list its signups.">
      <div className="stack">
        <div className="grid grid-stats">
          <StatCard label="Signups" value={n(t.signups)} sub={t.deleted_since ? `${n(t.deleted_since)} deleted since` : undefined} />
          <StatCard label="Average per day" value={t.average_per_day.toLocaleString("en-IN", { maximumFractionDigits: 1 })} />
          <StatCard label="Best day" value={t.best_day ? n(t.best_day_signups) : "—"} sub={t.best_day ? day(t.best_day, tz) : undefined} />
          <StatCard label="Have a course" value={n(t.with_course)} sub={`${pct(t.with_course, remaining)} of these signups`} />
          <StatCard label="Course purchases" value={n(t.purchases)}
            sub={`${money(t.revenue_paise, "INR")}${t.vouchers ? ` · ${n(t.vouchers)} by voucher` : ""}${t.other_currency_purchases ? ` · ${n(t.other_currency_purchases)} non-INR not in total` : ""}`} />
        </div>
        <DailyBars title={`New signups per day, ${data.days} days`} rows={data.daily} onSelectDay={onSelectDay} />
        <div className="grid grid-3">
          <BreakdownTable title="By class" rows={data.by_class} />
          <BreakdownTable title="By stream" rows={data.by_stream} />
          <BreakdownTable title="By goal" rows={data.by_goal} note="A student can pick several goals." />
        </div>
      </div>
    </Card>
  );
}

function BreakdownTable({ title, rows, note }: { title: string; rows: Breakdown[]; note?: string }) {
  const total = rows.reduce((sum, r) => sum + r.count, 0);
  return (
    <section aria-label={title}>
      <h3 className="stat-label">{title}</h3>
      {rows.length === 0 ? <p className="small muted">No signups in this range.</p> : (
        <table>
          <caption className="visually-hidden">Signups {title.toLowerCase()}</caption>
          <thead>
            <tr><th scope="col">{title.replace("By ", "")}</th><th scope="col" className="num">Signups</th><th scope="col" className="num">Share</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <td>{r.label}</td>
                <td className="num">{n(r.count)}</td>
                <td className="num">{total ? `${Math.round((r.count / total) * 100)}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {note && <p className="small muted">{note}</p>}
    </section>
  );
}
