// Course module helpers: labels, validity presets, course form state and validation (the
// server stays authoritative and returns field errors too). Dates use IST like the backend.

import type { StatusInfo } from "../../lib/status";
import { formatMoney } from "../../lib/format";
import { isoToIstInput, istInputToIso } from "../vouchers/shared";
import type { Course, CourseInput, DisplayStatus, EnrollmentStatus, ValidityUnit } from "./types";

export { IST, isoToIstInput, istInputToIso } from "../vouchers/shared";

export const STATUS_INFO: Record<DisplayStatus, StatusInfo> = {
  draft: { tone: "unavailable", label: "Draft" },
  scheduled: { tone: "info", label: "Scheduled" },
  published: { tone: "normal", label: "Published" },
  archived: { tone: "warning", label: "Archived" },
};

export const ENROLLMENT_INFO: Record<EnrollmentStatus, StatusInfo> = {
  active: { tone: "normal", label: "Active" },
  pending: { tone: "info", label: "Pending payment" },
  expired: { tone: "warning", label: "Expired" },
  cancelled: { tone: "unavailable", label: "Cancelled" },
  refunded: { tone: "critical", label: "Refunded" },
};

export const LANGUAGES: Record<string, string> = {
  en: "English", hi: "Hindi", bho: "Bhojpuri", mr: "Marathi", ta: "Tamil", te: "Telugu", bn: "Bengali",
  gu: "Gujarati", kn: "Kannada", ml: "Malayalam", pa: "Punjabi", or: "Odia", ur: "Urdu", as: "Assamese",
};

/** Validity presets offered in the form; "custom" opens value + unit. */
export const VALIDITY_PRESETS: { id: string; label: string; value: number; unit: ValidityUnit }[] = [
  { id: "6m", label: "6 months", value: 6, unit: "months" },
  { id: "7m", label: "7 months", value: 7, unit: "months" },
  { id: "10m", label: "10 months", value: 10, unit: "months" },
  { id: "1y", label: "1 year", value: 1, unit: "years" },
];
const MAX_VALIDITY: Record<ValidityUnit, number> = { days: 3650, months: 120, years: 10 };

export function priceLabel(c: Pick<Course, "is_free" | "currency" | "price" | "discounted_price" | "original_price">): string {
  if (c.is_free) return "Free";
  const sale = c.discounted_price ?? c.price;
  return formatMoney(sale, c.currency);
}

export function duration(seconds: number | null): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

export interface CourseForm {
  title: string;
  description: string;
  goals: string;
  thumbnail_asset_id: number | null;
  thumbnail_name: string | null;
  class_id: string;
  stream_id: string;
  subject_id: string;
  is_free: boolean;
  currency: string;
  price: string;
  original_price: string;
  discounted_price: string;
  launch_at: string; // datetime-local, IST
  validity_preset: string; // preset id or "custom"
  validity_value: string;
  validity_unit: ValidityUnit;
}

export function emptyCourseForm(): CourseForm {
  return {
    title: "", description: "", goals: "", thumbnail_asset_id: null, thumbnail_name: null, class_id: "", stream_id: "",
    subject_id: "", is_free: false, currency: "INR", price: "", original_price: "", discounted_price: "",
    launch_at: "", validity_preset: "6m", validity_value: "6", validity_unit: "months",
  };
}

export function formFromCourse(c: Course): CourseForm {
  const preset = VALIDITY_PRESETS.find((p) => p.value === c.validity_value && p.unit === c.validity_unit);
  return {
    title: c.title, description: c.description ?? "", goals: c.goals ?? "", thumbnail_asset_id: c.thumbnail?.id ?? null,
    thumbnail_name: c.thumbnail?.filename ?? null, class_id: c.class_id ? String(c.class_id) : "",
    stream_id: c.stream_id ? String(c.stream_id) : "", subject_id: c.subject_id ? String(c.subject_id) : "",
    is_free: c.is_free, currency: c.currency, price: c.price ?? "", original_price: c.original_price ?? "",
    discounted_price: c.discounted_price ?? "", launch_at: isoToIstInput(c.launch_at),
    validity_preset: preset ? preset.id : "custom", validity_value: String(c.validity_value), validity_unit: c.validity_unit,
  };
}

const MONEY = /^\d{1,9}(\.\d{1,2})?$/;

export function validateCourse(f: CourseForm): Record<string, string> {
  const e: Record<string, string> = {};
  if (f.title.trim().length < 2) e.title = "Enter a course name (2–160 characters).";
  if (f.description.length > 10_000) e.description = "At most 10,000 characters.";
  if (!f.is_free) {
    if (!MONEY.test(f.price.trim()) || Number(f.price) <= 0) e.price = "Enter a price above 0 (up to 2 decimals).";
    if (f.discounted_price.trim()) {
      if (!MONEY.test(f.discounted_price.trim())) e.discounted_price = "An amount with up to 2 decimals.";
      else if (!e.price && !(Number(f.discounted_price) > 0 && Number(f.discounted_price) < Number(f.price)))
        e.discounted_price = "Must be above 0 and below the course price.";
    }
    if (f.original_price.trim()) {
      if (!MONEY.test(f.original_price.trim())) e.original_price = "An amount with up to 2 decimals.";
      else if (!e.price && Number(f.original_price) < Number(f.price)) e.original_price = "The original price (MRP) can't be below the course price.";
    }
  }
  if (f.launch_at && !istInputToIso(f.launch_at)) e.launch_at = "Choose a valid date and time.";
  if (f.validity_preset === "custom") {
    const v = f.validity_value.trim();
    if (!/^\d+$/.test(v) || Number(v) < 1 || Number(v) > MAX_VALIDITY[f.validity_unit])
      e.validity_value = `A whole number from 1 to ${MAX_VALIDITY[f.validity_unit]} ${f.validity_unit}.`;
  }
  return e;
}

export function toCourseInput(f: CourseForm): CourseInput {
  const preset = VALIDITY_PRESETS.find((p) => p.id === f.validity_preset);
  const money = (v: string) => (v.trim() ? v.trim() : null);
  const id = (v: string) => (v ? Number(v) : null);
  return {
    title: f.title.trim(),
    description: f.description.trim() || null,
    goals: f.goals.trim() || null,
    thumbnail_asset_id: f.thumbnail_asset_id,
    class_id: id(f.class_id),
    stream_id: id(f.stream_id),
    subject_id: id(f.subject_id),
    is_free: f.is_free,
    currency: f.currency,
    price: f.is_free ? null : money(f.price),
    original_price: f.is_free ? null : money(f.original_price),
    discounted_price: f.is_free ? null : money(f.discounted_price),
    launch_at: f.launch_at ? istInputToIso(f.launch_at) : null,
    validity_value: preset ? preset.value : Number(f.validity_value),
    validity_unit: preset ? preset.unit : f.validity_unit,
  };
}
