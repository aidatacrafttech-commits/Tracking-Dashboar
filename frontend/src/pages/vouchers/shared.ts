// Voucher form state, validation that mirrors the AdhyaapakAI rules (the server stays
// authoritative), request bodies, and IST date handling.

import type { StatusInfo } from "../../lib/status";
import { formatMoney } from "../../lib/format";
import type { Category, DiscountType, PackageKey, Prefix, Voucher, VoucherStatus } from "./types";

export const IST = "Asia/Kolkata";

export const PREFIX: Record<Category, Prefix> = { course: "courses", live_tutor: "live-tutor" };

export const CATEGORY_INFO: Record<Category, { label: string; title: string; scope: string; all: string; item: string }> = {
  course: { label: "Course", title: "Course vouchers", scope: "Subjects", all: "All subjects", item: "subject" },
  live_tutor: { label: "Live Tutor", title: "Live Tutor vouchers", scope: "Packages", all: "All packages", item: "package" },
};

export const STATUS_INFO: Record<VoucherStatus, StatusInfo> = {
  active: { tone: "normal", label: "Active" },
  scheduled: { tone: "info", label: "Scheduled" },
  inactive: { tone: "unavailable", label: "Inactive" },
  expired: { tone: "warning", label: "Expired" },
};
export const STATUSES = Object.keys(STATUS_INFO) as VoucherStatus[];

export type DisplayStatusKey =
  | "deleted" | "inactive" | "expired" | "scheduled" | "fully_used" | "user_limit_reached" | "not_redeemable" | "active";

export interface DisplayStatus extends StatusInfo {
  key: DisplayStatusKey;
  /** Why the voucher can't be redeemed (shown as the badge tooltip). */
  title?: string;
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

/** What the status badge shows. The backend's `status` covers only the date window and the
 * on/off switch; a voucher whose limits are used up is still "active" but not `redeemable`.
 * First match wins. */
export function getVoucherDisplayStatus(
  v: Pick<Voucher, "deleted" | "status" | "redeemable" | "limits_remaining" | "total_limit" | "max_unique_users">,
): DisplayStatus {
  if (v.deleted) return { key: "deleted", tone: "unavailable", label: "Deleted" };
  if (v.status === "inactive") return { key: "inactive", tone: "unavailable", label: "Inactive" };
  if (v.status === "expired") return { key: "expired", tone: "critical", label: "Expired" };
  if (v.status === "scheduled") return { key: "scheduled", tone: "info", label: "Scheduled" };
  if (v.status === "active" && v.redeemable === false) {
    const remaining = v.limits_remaining ?? { total_remaining: null, unique_users_remaining: null };
    if (remaining.total_remaining === 0)
      return {
        key: "fully_used", tone: "warning", label: "Fully used",
        title: `All ${plural(v.total_limit ?? 0, "redemption")} ${v.total_limit === 1 ? "is" : "are"} used. Raise the total limit or create a new voucher.`,
      };
    if (remaining.unique_users_remaining === 0)
      return {
        key: "user_limit_reached", tone: "warning", label: "User limit reached",
        title: `All ${plural(v.max_unique_users ?? 0, "unique user")} ${v.max_unique_users === 1 ? "is" : "are"} used. Existing users may still redeem up to their per-user limit.`,
      };
    return { key: "not_redeemable", tone: "warning", label: "Not redeemable", title: "The backend reports this voucher cannot be redeemed right now." };
  }
  return { key: "active", tone: "normal", label: "Active" };
}

/** Client-side filter "Fully used": vouchers whose limits are exhausted. */
export const isExhausted = (v: Parameters<typeof getVoucherDisplayStatus>[0]) =>
  ["fully_used", "user_limit_reached"].includes(getVoucherDisplayStatus(v).key);

export const PACKAGE_KEYS: PackageKey[] = ["beginner", "intermediate", "pro"];
export const PACKAGE_LABELS: Record<PackageKey, string> = { beginner: "Beginner", intermediate: "Intermediate", pro: "Pro" };

export const rupees = (n: number | null | undefined) => (n === null || n === undefined ? "—" : formatMoney(String(n), "INR"));

export function discountLabel(v: Pick<Voucher, "discount_type" | "discount_value" | "max_discount_inr">): string {
  if (v.discount_type === "fixed") return `${rupees(v.discount_value)} off`;
  return `${v.discount_value}% off${v.max_discount_inr ? ` (max ${rupees(v.max_discount_inr)})` : ""}`;
}

export function scopeLabel(v: Voucher, names: Record<string, string>): string {
  if (v.category === "course") return v.all_subjects ? "All subjects" : (v.subject_keys ?? []).map((k) => names[k] ?? k).join(", ");
  return v.all_packages ? "All packages" : (v.package_keys ?? []).map((k) => names[k] ?? PACKAGE_LABELS[k] ?? k).join(", ");
}

// --- IST <-> API ------------------------------------------------------------------------------

/** API instant ("...Z") -> "YYYY-MM-DDTHH:mm" wall time in IST for a datetime-local input. */
export function isoToIstInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: IST, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).formatToParts(new Date(iso)).map((p) => [p.type, p.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

/** datetime-local value (IST wall time) -> ISO with the required +05:30 offset. */
export function istInputToIso(local: string): string | null {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local) ? `${local}:00+05:30` : null;
}

const toMinute = (iso: string | null | undefined) => (iso ? Math.floor(Date.parse(iso) / 60_000) : null);

// --- form ------------------------------------------------------------------------------------

export interface VoucherForm {
  code: string;
  title: string;
  description: string;
  discount_type: DiscountType;
  discount_value: string;
  max_discount_inr: string;
  min_purchase_inr: string;
  starts_at: string;
  ends_at: string;
  benefit_validity_days: string;
  per_user_limit: string;
  max_unique_users: string;
  total_limit: string;
  enabled: boolean;
  all: boolean;
  keys: string[];
}

/** Server field name -> form field (for 422 messages). */
export const FIELD_ALIASES: Record<string, keyof VoucherForm> = {
  all_subjects: "keys", subject_keys: "keys", all_packages: "keys", package_keys: "keys",
};

export function emptyForm(nowIst: string): VoucherForm {
  return {
    code: "", title: "", description: "", discount_type: "percent", discount_value: "", max_discount_inr: "",
    min_purchase_inr: "", starts_at: nowIst, ends_at: "", benefit_validity_days: "", per_user_limit: "1",
    max_unique_users: "", total_limit: "", enabled: true, all: false, keys: [],
  };
}

export function formFromVoucher(v: Voucher): VoucherForm {
  const n = (x: number | null | undefined) => (x === null || x === undefined ? "" : String(x));
  return {
    code: v.code,
    title: v.title ?? "",
    description: v.description ?? "",
    discount_type: v.discount_type,
    discount_value: String(v.discount_value),
    max_discount_inr: n(v.max_discount_inr),
    min_purchase_inr: v.min_purchase_inr ? String(v.min_purchase_inr) : "",
    starts_at: isoToIstInput(v.starts_at),
    ends_at: isoToIstInput(v.ends_at),
    benefit_validity_days: n(v.benefit_validity_days),
    per_user_limit: String(v.per_user_limit ?? 1),
    max_unique_users: n(v.max_unique_users),
    total_limit: n(v.total_limit),
    enabled: v.enabled,
    all: (v.category === "course" ? v.all_subjects : v.all_packages) ?? false,
    keys: [...((v.category === "course" ? v.subject_keys : v.package_keys) ?? [])],
  };
}

const CODE_RE = /^[A-Z0-9_-]{3,32}$/;

function int(value: string, min: number, max = Number.MAX_SAFE_INTEGER): number | null | "invalid" {
  const v = value.trim();
  if (!v) return null;
  return /^\d+$/.test(v) && Number(v) >= min && Number(v) <= max ? Number(v) : "invalid";
}

export function validateForm(form: VoucherForm, category: Category): Record<string, string> {
  const e: Record<string, string> = {};
  if (!CODE_RE.test(form.code.trim().toUpperCase())) e.code = "3–32 characters: A–Z, 0–9, - or _.";
  if (form.title.length > 120) e.title = "At most 120 characters.";
  if (form.description.length > 1000) e.description = "At most 1000 characters.";
  const percent = form.discount_type === "percent";
  const value = int(form.discount_value, 1, percent ? 100 : Number.MAX_SAFE_INTEGER);
  if (value === null) e.discount_value = "Enter the discount.";
  else if (value === "invalid") e.discount_value = percent ? "A whole number from 1 to 100." : "Whole rupees, 1 or more.";
  if (percent && int(form.max_discount_inr, 1) === "invalid") e.max_discount_inr = "Whole rupees, 1 or more, or empty.";
  if (int(form.min_purchase_inr, 0) === "invalid") e.min_purchase_inr = "Whole rupees, 0 or more, or empty.";
  if (!istInputToIso(form.starts_at)) e.starts_at = "Choose when the voucher starts.";
  if (form.ends_at && !e.starts_at && form.ends_at <= form.starts_at) e.ends_at = "Must be after the start.";
  if (int(form.benefit_validity_days, 1, 3650) === "invalid") e.benefit_validity_days = "1 to 3650 days, or empty.";
  if (int(form.per_user_limit, 1) === null || int(form.per_user_limit, 1) === "invalid") e.per_user_limit = "A whole number, 1 or more.";
  if (int(form.max_unique_users, 1) === "invalid") e.max_unique_users = "A whole number, 1 or more, or empty for unlimited.";
  if (int(form.total_limit, 1) === "invalid") e.total_limit = "A whole number, 1 or more, or empty for unlimited.";
  if (!form.all && form.keys.length === 0)
    e.keys = `Choose at least one ${CATEGORY_INFO[category].item}, or ${CATEGORY_INFO[category].all.toLowerCase()}.`;
  return e;
}

/** Every field as the API would store it (null = not set). */
function normalized(form: VoucherForm, category: Category): Record<string, unknown> {
  const n = (v: string) => (v.trim() ? Number(v.trim()) : null);
  const percent = form.discount_type === "percent";
  const [allField, keysField] = category === "course" ? ["all_subjects", "subject_keys"] : ["all_packages", "package_keys"];
  return {
    code: form.code.trim().toUpperCase(),
    title: form.title.trim() || null,
    description: form.description.trim() || null,
    discount_type: form.discount_type,
    discount_value: n(form.discount_value),
    max_discount_inr: percent ? n(form.max_discount_inr) : null,
    min_purchase_inr: n(form.min_purchase_inr) ?? 0,
    starts_at: istInputToIso(form.starts_at),
    ends_at: form.ends_at ? istInputToIso(form.ends_at) : null,
    benefit_validity_days: n(form.benefit_validity_days),
    per_user_limit: n(form.per_user_limit) ?? 1,
    max_unique_users: n(form.max_unique_users),
    total_limit: n(form.total_limit),
    enabled: form.enabled,
    [allField]: form.all,
    [keysField]: form.all ? [] : form.keys,
  };
}

/** POST body: optional fields that are empty are omitted (never sent as 0 or ""). */
export function createBody(form: VoucherForm, category: Category): Record<string, unknown> {
  const body = normalized(form, category);
  for (const key of ["title", "description", "max_discount_inr", "ends_at", "benefit_validity_days", "max_unique_users", "total_limit"])
    if (body[key] === null) delete body[key];
  if (body.min_purchase_inr === 0) delete body.min_purchase_inr;
  return body;
}

/** PATCH body: only fields that differ from the saved voucher; null clears a field. */
export function patchBody(form: VoucherForm, category: Category, original: Voucher): Record<string, unknown> {
  const next = normalized(form, category);
  const before = normalized(formFromVoucher(original), category);
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(next)) {
    const old = key === "starts_at" || key === "ends_at" ? toMinute(original[key]) : before[key];
    const now = key === "starts_at" || key === "ends_at" ? toMinute(value as string | null) : value;
    const same = Array.isArray(now) && Array.isArray(old)
      ? now.length === old.length && [...now].sort().join("\u0000") === [...old].sort().join("\u0000")
      : now === old;
    if (!same) out[key] = value;
  }
  return out;
}

// --- redemptions ------------------------------------------------------------------------------

/** "class12_accountancy" -> "Class 12 Accountancy", "beginner" -> "Beginner". */
export function prettyKey(key: string): string {
  return key
    .replace(/([a-z])(\d)/gi, "$1 $2")
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Subject / package name from the backend's lists, else a readable form of the key. */
export const targetLabel = (key: string, names: Record<string, string>) => names[key] ?? prettyKey(key);

export const redemptionDate = (r: { completed_at: string | null; reserved_at: string | null }) => r.completed_at ?? r.reserved_at;

function csvCell(value: string): string {
  // Neutralise spreadsheet formulas (but keep plain numbers such as "+919800000001"), then quote.
  const formula = /^[=+\-@\t\r]/.test(value) && !/^[+-]?[\d\s()-]+$/.test(value);
  const safe = formula ? `'${value}` : value;
  return `"${safe.replace(/"/g, '""')}"`;
}

/** Download rows as CSV straight from memory: nothing is written to storage. */
export function downloadCsv(filename: string, header: string[], rows: string[][]): void {
  const text = "﻿" + [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
