import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import type { Decimal } from "../api/types";
import { formatAge, formatAmount } from "../lib/format";
import type { StatusInfo, Tone } from "../lib/status";
import { IconCheck, IconClock, IconCritical, IconInfo, IconMinus, IconWarning } from "./icons";

const TONE_ICONS: Record<Tone, (p: { size?: number }) => ReactNode> = {
  normal: IconCheck,
  warning: IconWarning,
  critical: IconCritical,
  unavailable: IconMinus,
  info: IconInfo,
};

export function Badge({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  const Icon = TONE_ICONS[tone];
  return (
    <span className={`badge tone-${tone}`} title={title}>
      <Icon size={12} />
      {children}
    </span>
  );
}

export function StatusBadge({ status, title }: { status: StatusInfo; title?: string }) {
  return (
    <Badge tone={status.tone} title={title}>
      {status.label}
    </Badge>
  );
}

export function Amount({ value, unit, className }: { value: Decimal | null | undefined; unit: string; className?: string }) {
  return <span className={className}>{formatAmount(value, unit)}</span>;
}

export function Card({
  title,
  description,
  actions,
  children,
  className = "",
  headingLevel = 2,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  headingLevel?: 2 | 3;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-header">
          <div>
            {title && <Heading>{title}</Heading>}
            {description && <p>{description}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function StatCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: Tone;
}) {
  return (
    <section className="card" aria-label={label}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone === "critical" ? "text-critical" : tone === "warning" ? "text-warning" : ""}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </section>
  );
}

export function Skeleton({ height = 16, width = "100%" }: { height?: number; width?: number | string }) {
  return <div className="skeleton" style={{ height, width }} aria-hidden="true" />;
}

export function LoadingBlock({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="stack" role="status" aria-live="polite">
      <span className="visually-hidden">{label}…</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={i === 0 ? 22 : 14} width={i === 0 ? "40%" : `${90 - i * 10}%`} />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message =
    error instanceof ApiError
      ? error.message
      : "Something went wrong while loading this section.";
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="notice tone-critical" role="alert">
      <IconCritical />
      <div className="stack">
        <p>
          <strong>Could not load data.</strong> {message}
        </p>
        {requestId && <p className="small muted">Request ID: <code>{requestId}</code></p>}
        {onRetry && (
          <div>
            <button type="button" className="btn btn-sm" onClick={onRetry}>
              Try again
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function Notice({ tone, children }: { tone: Tone; children: ReactNode }) {
  const Icon = TONE_ICONS[tone];
  return (
    <div className={`notice tone-${tone}`} role={tone === "critical" ? "alert" : "note"}>
      <Icon />
      <div>{children}</div>
    </div>
  );
}

/** Freshness label that turns into a warning once data is older than the limit. */
export function Freshness({ iso, stale, label = "Updated" }: { iso: string | null | undefined; stale?: boolean | null; label?: string }) {
  if (!iso) return <Badge tone="unavailable">No data yet</Badge>;
  if (stale)
    return (
      <Badge tone="warning" title="Older than this provider's freshness limit; not used for alerts">
        Stale · {formatAge(iso)}
      </Badge>
    );
  return (
    <span className="small muted row" style={{ gap: 4 }}>
      <IconClock size={13} />
      {label} {formatAge(iso)}
    </span>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}
