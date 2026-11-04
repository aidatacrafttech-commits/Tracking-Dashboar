import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAppSettings, useOpsStatus } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { useLiveState } from "../live/LiveUpdates";
import { IconAlerts, IconBook, IconCloud, IconLogo, IconMenu, IconOverview, IconPayments, IconProviders, IconThresholds, IconTicket, IconUsers } from "./icons";

const NAV = [
  { to: "/", label: "Overview", icon: IconOverview, end: true },
  { to: "/providers", label: "Providers", icon: IconProviders },
  { to: "/payments", label: "Payments", icon: IconPayments },
  { to: "/thresholds", label: "Thresholds", icon: IconThresholds },
  { to: "/alerts", label: "Alerts", icon: IconAlerts },
  { to: "/aws", label: "AWS", icon: IconCloud },
  { to: "/vouchers", label: "Voucher Management", icon: IconTicket },
  { to: "/courses", label: "Course Management", icon: IconBook },
  { to: "/app-users", label: "App Users", icon: IconUsers },
];

const LIVE_LABELS = {
  live: "Live updates on",
  connecting: "Connecting…",
  reconnecting: "Reconnecting…",
  paused: "Paused (tab hidden)",
  offline: "Live updates off",
} as const;

export function useDisplayTimezone(): string {
  const { data } = useAppSettings();
  return data?.display_timezone ?? "UTC";
}

export function Layout() {
  const { role, canWrite, signOut } = useAuth();
  const ops = useOpsStatus(canWrite);
  const live = useLiveState();
  const tz = useDisplayTimezone();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {open && <button type="button" className="scrim" aria-label="Close menu" onClick={() => setOpen(false)} />}
      <aside className="sidebar" data-open={open} aria-label="Main navigation" id="sidebar">
        <div className="brand">
          <IconLogo />
          <span>Adhyaapak AI Dashboard</span>
        </div>
        <nav className="nav">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)}>
              <Icon />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="row" title="Server-Sent Events channel status">
            <span className="live-dot" data-state={live} aria-hidden="true" />
            <span role="status">{LIVE_LABELS[live]}</span>
          </span>
          <span>Times shown in {tz}</span>
          <span>
            Signed in as <strong>{role === "owner" ? "Owner" : "Viewer (read-only)"}</strong>
          </span>
          <button type="button" className="btn btn-sm" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="btn btn-ghost icon-btn"
            aria-label="Open menu"
            aria-expanded={open}
            aria-controls="sidebar"
            onClick={() => setOpen(true)}
          >
            <IconMenu size={20} />
          </button>
          <IconLogo size={22} />
          <strong>Adhyaapak AI Dashboard</strong>
          <span className="live-dot" data-state={live} title={LIVE_LABELS[live]} style={{ marginLeft: "auto" }} />
        </header>
        <main id="main" className="content" tabIndex={-1}>
          {ops.data && ops.data.status !== "ok" && (
            <div className={`notice tone-${ops.data.status === "critical" ? "critical" : "warning"}`} role="alert">
              <div>
                <strong>{ops.data.status === "critical" ? "Action needed:" : "Attention:"}</strong>{" "}
                {ops.data.problems.join(" · ")}
              </div>
            </div>
          )}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
