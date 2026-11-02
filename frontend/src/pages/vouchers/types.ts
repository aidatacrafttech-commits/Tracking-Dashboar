// Shapes returned by the AdhyaapakAI voucher admin API (see docs/VOUCHERS.md).

export type Category = "course" | "live_tutor";
export type Prefix = "courses" | "live-tutor";
export type VoucherStatus = "scheduled" | "active" | "inactive" | "expired";
export type DiscountType = "percent" | "fixed";
export type PackageKey = "beginner" | "intermediate" | "pro";

export interface Voucher {
  id: string;
  category: Category;
  code: string;
  title: string | null;
  description: string | null;
  discount_type: DiscountType;
  discount_value: number;
  max_discount_inr: number | null;
  min_purchase_inr: number | null;
  starts_at: string;
  ends_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
  benefit_validity_days: number | null;
  per_user_limit: number;
  max_unique_users: number | null;
  total_limit: number | null;
  enabled: boolean;
  all_subjects?: boolean;
  subject_keys?: string[];
  all_packages?: boolean;
  package_keys?: PackageKey[];
  status: VoucherStatus;
  redeemable: boolean;
  stats: { total_redemptions: number; completed_redemptions: number; unique_users: number };
  limits_remaining: { total_remaining: number | null; unique_users_remaining: number | null };
  deleted: boolean;
  original_code?: string | null;
}

export interface VoucherList {
  vouchers: Voucher[];
  total: number;
}

export interface Redemption {
  id: string;
  voucher_code: string;
  category: Category;
  user_id: string;
  target_type: string;
  target_id: string;
  product_id: string | null;
  original_amount_inr: number;
  discount_inr: number;
  final_amount_inr: number;
  is_free: boolean;
  status: "pending" | "completed" | "cancelled" | "refunded";
  reserved_at: string | null;
  completed_at: string | null;
  order_id?: string | null;
  payment_id?: string | null;
  benefit_expires_at: string | null;
  late?: boolean;
  /** Who redeemed it. name/email/phone are null when the account no longer exists. */
  user?: { id: string; name: string | null; email: string | null; phone: string | null } | null;
}

export interface Subject {
  key: string;
  name: string;
  class_level: number | null;
  subject: string | null;
  product_ids: string[];
  builtin: boolean;
  active: boolean;
}

export interface TutorPackage {
  key: PackageKey;
  name: string;
  price_inr: number;
  tokens: number;
  active: boolean;
  sort_order: number;
  token_pack_product_id: string | null;
  linked: boolean;
}
