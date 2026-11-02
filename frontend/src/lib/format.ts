// Formatting helpers. Amounts arrive as exact decimal strings and are formatted from the
// string itself (Intl.NumberFormat accepts numeric strings), so no float rounding occurs.

import type { Decimal } from "../api/types";

const CURRENCY_RE = /^[A-Z]{3}$/;
const nfCache = new Map<string, Intl.NumberFormat>();

function nf(key: string, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  let f = nfCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(undefined, options);
    nfCache.set(key, f);
  }
  return f;
}

type NumericInput = Parameters<Intl.NumberFormat["format"]>[0];

function fmt(f: Intl.NumberFormat, value: Decimal): string {
  // A decimal string is a valid Intl "StringNumericLiteral": exact, no float conversion.
  return f.format(value as unknown as NumericInput);
}

export function isCurrency(unit: string | null | undefined): boolean {
  return !!unit && CURRENCY_RE.test(unit);
}

/** Money in its own currency, e.g. "$1,250.75" / "₹2,000.00". */
export function formatMoney(value: Decimal | null | undefined, currency: string): string {
  if (value === null || value === undefined) return "Unknown";
  try {
    return fmt(nf(`money:${currency}`, { style: "currency", currency, minimumFractionDigits: 2 }), value);
  } catch {
    return `${value} ${currency}`;
  }
}

const UNIT_LABELS: Record<string, string> = {
  CREDITS: "credits",
  tokens: "tokens",
  requests: "requests",
  hours: "hours",
  characters: "characters",
  GB: "GB",
  objects: "objects",
};

/** Any value with its unit: money for ISO currencies, otherwise "9,000 credits" etc. */
export function formatAmount(value: Decimal | null | undefined, unit: string): string {
  if (value === null || value === undefined) return "Unknown";
  if (isCurrency(unit)) return formatMoney(value, unit);
  const digits = unit === "hours" || unit === "GB" ? 2 : 0;
  return `${fmt(nf(`qty:${digits}`, { maximumFractionDigits: digits }), value)} ${UNIT_LABELS[unit] ?? unit}`;
}

/** Signed difference such as "+$12.40" / "−$6.60". */
export function formatSigned(value: Decimal, unit: string): string {
  const negative = value.trim().startsWith("-");
  const magnitude = negative ? value.trim().slice(1) : value.trim();
  const sign = negative ? "−" : value.replace(/[0.]/g, "") === "" ? "" : "+";
  return `${sign}${formatAmount(magnitude, unit)}`;
}

/** Compact axis labels for charts (visual only; never used for money comparisons). */
export function formatAxis(value: number, unit: string): string {
  const compact = nf("compact", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  if (isCurrency(unit)) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: unit,
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(value);
    } catch {
      return compact;
    }
  }
  return compact;
}

const dtfCache = new Map<string, Intl.DateTimeFormat>();

function dtf(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}:${JSON.stringify(options)}`;
  let f = dtfCache.get(key);
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(undefined, { ...options, timeZone });
    } catch {
      f = new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" });
    }
    dtfCache.set(key, f);
  }
  return f;
}

/** Date-time in the configured display timezone, e.g. "13 Oct 2026, 11:56 IST". */
export function formatDateTime(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "Never";
  return dtf(timeZone, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(iso));
}

export function formatDate(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "—";
  return dtf(timeZone, { day: "numeric", month: "short", year: "numeric" }).format(new Date(iso));
}

/** Short day label for charts. Daily buckets are UTC days, so label them in UTC. */
export function formatDayLabel(iso: string): string {
  return dtf("UTC", { day: "numeric", month: "short" }).format(new Date(iso));
}

/** "4 min ago", "3 h ago", "2 days ago". */
export function formatAge(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
