// Types mirroring the backend's Pydantic response models (backend/app/schemas).
// Monetary and quantity values are decimal *strings*: the UI never does money math.

export type Decimal = string;
export type IsoDateTime = string;

export type Role = "owner" | "viewer";
export type SourceType = "api_verified" | "usage_estimate" | "manual" | "unavailable";
export type DataLatency = "realtime" | "delayed";
export type AlertState = "unknown" | "ok" | "breached";
export type EvaluationStatus =
  | "ok"
  | "breached"
  | "no_data"
  | "stale"
  | "currency_mismatch"
  | "incompatible_units"
  | "disabled";
export type SyncStatus = "ok" | "partial" | "error" | "unsupported" | "not_configured" | "disabled";
export type Operation = "balance" | "usage" | "billing" | "health" | "resources";
export type DeliveryStatus = "pending" | "sending" | "sent" | "failed" | "suppressed";
export type AlertEventType = "breached" | "reminder" | "recovered" | "test";
export type PaymentKind = "top_up" | "subscription" | "invoice_payment" | "refund" | "other";
export type EntryMethod = "manual" | "provider_verified";
export type BalanceKind = "prepaid_balance" | "plan_quota" | "promotional_credit";
export type BalanceCapability = "api" | "partial" | "manual_only" | "postpaid";
export type HealthStatus =
  | "operational"
  | "degraded"
  | "partial_outage"
  | "major_outage"
  | "maintenance"
  | "unknown";
export type EligibleSource = "api_verified" | "manual" | "usage_estimate";

export interface ApiErrorBody {
  error: { code: string; message: string; request_id: string | null; details?: unknown };
}

export interface SessionInfo {
  role: Role;
  can_write: boolean;
}

export interface AppSettings {
  display_timezone: string;
  supported_currencies: string[];
}

export interface BalanceSnapshot {
  id: number;
  balance_kind: BalanceKind;
  amount: Decimal | null;
  unit: string;
  unit_type: "currency" | "credits";
  source: "provider_api" | "billing_report" | "usage_estimate" | "manual";
  source_type: SourceType;
  data_latency: DataLatency;
  observed_at: IsoDateTime;
  data_as_of: IsoDateTime | null;
  unavailable_reason: string | null;
  monitoring_run_id: number | null;
  details: Record<string, unknown> | null;
}

export interface Freshness {
  observed_at: IsoDateTime;
  data_as_of: IsoDateTime | null;
  age_seconds: number;
  is_stale: boolean;
}

export interface Capability {
  operation: Operation;
  supported: boolean;
  note: string;
}

export interface Integration {
  status: SyncStatus;
  missing_config: string[];
  note: string | null;
  capabilities: Capability[];
}

export interface ThresholdHeadroom {
  amount: Decimal;
  unit: string;
  position: "above" | "at" | "below";
  balance_source_type: SourceType;
  is_stale: boolean;
}

export interface ThresholdSummary {
  amount: Decimal;
  unit: string;
  is_enabled: boolean;
  alert_state: AlertState;
  last_evaluation_status: EvaluationStatus | null;
}

export interface Provider {
  id: string;
  display_name: string;
  balance_capability: BalanceCapability;
  balance_capability_note: string;
  default_unit: string;
  default_unit_type: "currency" | "credits";
  stale_after_minutes: number;
  status_page_url: string | null;
  is_enabled: boolean;
  latest_balance: BalanceSnapshot | null;
  latest_balance_freshness: Freshness | null;
  threshold: ThresholdSummary | null;
  integration: Integration | null;
  threshold_headroom: ThresholdHeadroom | null;
}

export interface Threshold {
  provider: string;
  balance_kind: BalanceKind;
  amount: Decimal;
  unit: string;
  unit_type: "currency" | "credits";
  is_enabled: boolean;
  notify_on_breach: boolean;
  notify_on_recovery: boolean;
  recovery_margin: Decimal;
  recovery_boundary: Decimal;
  cooldown_minutes: number | null;
  notification_emails: string[] | null;
  effective_recipients: string[];
  eligible_sources: EligibleSource[];
  monitoring_interval_minutes: number | null;
  alert_state: AlertState;
  breach_episode: number;
  state_changed_at: IsoDateTime | null;
  last_evaluated_at: IsoDateTime | null;
  last_evaluation_status: EvaluationStatus | null;
  last_evaluation_message: string | null;
  last_notified_at: IsoDateTime | null;
  email_delivery_enabled: boolean;
  version: number;
  updated_at: IsoDateTime;
}

export interface ThresholdInput {
  amount: Decimal;
  unit: string;
  is_enabled: boolean;
  notify_on_breach: boolean;
  notify_on_recovery: boolean;
  recovery_margin: Decimal;
  cooldown_minutes: number | null;
  notification_emails: string[] | null;
  eligible_sources: EligibleSource[];
  monitoring_interval_minutes: number | null;
}

export interface Payment {
  id: number;
  provider: string;
  kind: PaymentKind;
  amount: Decimal;
  currency: string;
  transaction_date: IsoDateTime;
  payment_reference: string | null;
  external_id: string | null;
  entry_method: EntryMethod;
  verified_at: IsoDateTime | null;
  notes: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime | null;
  reconciled_at: IsoDateTime | null;
  reconciled_balance_snapshot_id: number | null;
  editable: boolean;
}

export interface PaymentInput {
  amount: Decimal;
  currency: string;
  transaction_date: IsoDateTime;
  kind: PaymentKind;
  payment_reference: string | null;
  notes: string | null;
}

export interface CurrencyTotal {
  currency: string;
  net_amount: Decimal;
  count: number;
}

export interface PaymentPage {
  items: Payment[];
  total: number;
  limit: number;
  offset: number;
  totals: CurrencyTotal[];
}

export interface ReconcilePreview {
  payment_id: number;
  allowed: boolean;
  reason: string | null;
  base: {
    snapshot_id: number;
    amount: Decimal;
    unit: string;
    source: string;
    observed_at: IsoDateTime;
  } | null;
  payment_effect: Decimal | null;
  resulting_balance: Decimal | null;
  unit: string | null;
}

export interface SeriesPoint {
  period_start: IsoDateTime;
  period_end: IsoDateTime;
  value: Decimal;
  is_final: boolean;
}

export interface Series {
  metric: string;
  unit: string;
  dimension: string;
  provider?: string | null;
  points: SeriesPoint[];
}

export interface MetricTotal {
  metric: string;
  unit: string;
  value: Decimal;
  dimension: string;
  observed_at: IsoDateTime | null;
  is_final: boolean | null;
  period_start?: IsoDateTime | null;
}

export interface UsageSeries {
  provider: string;
  days: number;
  since: IsoDateTime;
  series: Series[];
  month_to_date: MetricTotal[];
  latest: MetricTotal[];
}

export interface SyncState {
  operation: Operation;
  supported: boolean;
  status: SyncStatus | null;
  last_attempt_at: IsoDateTime | null;
  last_successful_sync: IsoDateTime | null;
  seconds_since_success: number | null;
  is_stale: boolean;
  consecutive_failures: number;
  error_code: string | null;
  error_message: string | null;
}

export interface ProviderSync {
  provider: string;
  running: boolean;
  integration: Integration;
  operations: SyncState[];
}

export interface SyncRun {
  provider: string;
  skipped: boolean;
  reason: string | null;
  run_id: number | null;
  status: "running" | "succeeded" | "partial" | "failed" | null;
  records_written: number;
  operations: {
    operation: Operation;
    status: SyncStatus;
    error_code: string | null;
    error_message: string | null;
    warnings: string[];
  }[];
}

export interface HealthCheck {
  id: number;
  source: "status_page" | "api_probe";
  status: HealthStatus;
  indicator: string | null;
  description: string | null;
  http_status: number | null;
  latency_ms: number | null;
  error_message: string | null;
  checked_at: IsoDateTime;
}

export interface Resource {
  resource_type: "ec2_instance" | "ebs_volume" | string;
  resource_id: string;
  region: string;
  name: string | null;
  state: string | null;
  health_status: string | null;
  details: Record<string, unknown> | null;
  observed_at: IsoDateTime;
}

export interface ResourceList {
  items: Resource[];
  observed_at: IsoDateTime | null;
  is_stale: boolean;
}

export interface DeliveryAttempt {
  attempt_number: number;
  outcome: "sent" | "retry" | "failed";
  error_code: string | null;
  error_message: string | null;
  smtp_status: number | null;
  started_at: IsoDateTime;
  finished_at: IsoDateTime;
}

export interface AlertEvent {
  id: number;
  provider: string;
  event_type: AlertEventType;
  episode: number | null;
  observed_amount: Decimal | null;
  threshold_amount: Decimal;
  unit: string;
  recipients: string[] | null;
  delivery_status: DeliveryStatus;
  delivery_attempts: number;
  last_error: string | null;
  subject: string | null;
  dedupe_key: string;
  occurred_at: IsoDateTime;
  next_attempt_at: IsoDateTime | null;
  sent_at: IsoDateTime | null;
  source_type: SourceType | null;
  attempts: DeliveryAttempt[];
}

export interface AlertEventDetail extends AlertEvent {
  body_text: string | null;
  body_html: string | null;
  context: Record<string, unknown> | null;
}

export interface AlertPage {
  items: AlertEvent[];
  total: number;
  limit: number;
  offset: number;
}

export interface EmailPreview {
  provider: string;
  event_type: AlertEventType;
  subject: string;
  text: string;
  html: string;
  recipients: string[];
  email_delivery_enabled: boolean;
  note: string;
}

export interface NotificationStatus {
  email_delivery_enabled: boolean;
  smtp_configured: boolean;
  smtp_host: string | null;
  smtp_port: number;
  smtp_security: "starttls" | "ssl" | "none";
  smtp_from: string | null;
  smtp_auth_configured: boolean;
  default_recipients: string[];
  dashboard_url: string | null;
  exchange_rates_configured: boolean;
  exchange_rate_source: string | null;
  outbox: Record<DeliveryStatus, number>;
}

export interface TestEmailResult {
  event_id: number;
  recipients: string[];
  delivery_status: DeliveryStatus | null;
  error_code: string | null;
  error_message: string | null;
}

export interface EvaluationResult {
  provider: string;
  status: EvaluationStatus;
  message: string;
  alert_state: AlertState;
  balance: Decimal | null;
  unit: string | null;
  snapshot_id: number | null;
  events_created: number[];
  skipped_concurrent: boolean;
}

export interface OverviewProvider {
  id: string;
  display_name: string;
  balance_capability?: BalanceCapability | null;
  integration_status: SyncStatus | null;
  alert_state: AlertState | null;
  evaluation_status: EvaluationStatus | null;
  balance: Decimal | null;
  balance_unit: string | null;
  balance_source_type: SourceType | null;
  balance_observed_at: IsoDateTime | null;
  balance_is_stale: boolean | null;
  threshold: Decimal | null;
  threshold_unit: string | null;
  headroom: ThresholdHeadroom | null;
  last_successful_sync: IsoDateTime | null;
  failing_operations: string[];
  last_error_code: string | null;
  /** Provider-reported spend since the 1st of this month (UTC), one entry per currency. */
  month_to_date_cost: MetricTotal[];
  /** This month's headline usage (e.g. ElevenLabs credits) when no money is reported. */
  month_to_date_usage?: MetricTotal[];
  /** false: the provider has no usage or billing API at all. */
  spend_reported?: boolean | null;
}

export interface Overview {
  generated_at: IsoDateTime;
  counts: {
    providers: number;
    low_balance: number;
    integration_failures: number;
    not_configured: number;
    stale_balances: number;
  };
  last_successful_sync: IsoDateTime | null;
  providers: OverviewProvider[];
  aws_month_to_date: MetricTotal[];
  aws_month_forecast: MetricTotal[];
  recent_payments: Payment[];
  recent_alerts: AlertEvent[];
  spend_trend: Series[];
  usage_trend: Series[];
}
