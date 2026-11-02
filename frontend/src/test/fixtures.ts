// Test fixtures shaped exactly like backend responses. Used only by unit tests.

import type {
  AlertEvent,
  Capability,
  AlertPage,
  Overview,
  Payment,
  PaymentPage,
  Provider,
  ProviderSync,
  Threshold,
} from "../api/types";

export const NOW = "2026-10-14T06:00:00Z";
export const OWNER = "owner-token-0123456789-abcdefghijklmnopqrstu";
export const VIEWER = "viewer-token-0123456789-abcdefghijklmnopqrstu";

const caps: Capability[] = [
  { operation: "balance", supported: true, note: "Project balances API" },
  { operation: "usage", supported: true, note: "Usage breakdown" },
];

export function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: "deepgram",
    display_name: "Deepgram",
    balance_capability: "api",
    balance_capability_note: "Actual balance is reported by the provider API.",
    default_unit: "USD",
    default_unit_type: "currency",
    stale_after_minutes: 120,
    status_page_url: "https://status.deepgram.com",
    is_enabled: true,
    latest_balance: {
      id: 1,
      balance_kind: "prepaid_balance",
      amount: "18.4",
      unit: "USD",
      unit_type: "currency",
      source: "provider_api",
      source_type: "api_verified",
      data_latency: "realtime",
      observed_at: NOW,
      data_as_of: null,
      unavailable_reason: null,
      monitoring_run_id: 1,
      details: null,
    },
    latest_balance_freshness: { observed_at: NOW, data_as_of: null, age_seconds: 60, is_stale: false },
    threshold: { amount: "25", unit: "USD", is_enabled: true, alert_state: "breached", last_evaluation_status: "breached" },
    integration: { status: "ok", missing_config: [], note: null, capabilities: caps },
    threshold_headroom: { amount: "-6.6", unit: "USD", position: "below", balance_source_type: "api_verified", is_stale: false },
    ...overrides,
  };
}

export const providers: Provider[] = [
  provider(),
  provider({
    id: "openai",
    display_name: "OpenAI",
    balance_capability: "manual_only",
    latest_balance: null,
    latest_balance_freshness: null,
    threshold: null,
    threshold_headroom: null,
    integration: { status: "not_configured", missing_config: ["OPENAI_ADMIN_KEY"], note: null, capabilities: caps },
  }),
  provider({
    id: "sarvam",
    display_name: "Sarvam AI",
    balance_capability: "manual_only",
    default_unit: "INR",
    latest_balance: { ...provider().latest_balance!, id: 3, amount: "2500", unit: "INR", source: "manual", source_type: "manual" },
    latest_balance_freshness: { observed_at: "2026-10-10T06:00:00Z", data_as_of: null, age_seconds: 345600, is_stale: true },
    threshold: null,
    threshold_headroom: null,
  }),
];

export function payment(overrides: Partial<Payment> = {}): Payment {
  return {
    id: 1,
    provider: "openai",
    kind: "top_up",
    amount: "50",
    currency: "USD",
    transaction_date: "2026-10-10T10:00:00Z",
    payment_reference: "INV-1",
    external_id: null,
    entry_method: "manual",
    verified_at: null,
    notes: "card",
    created_at: NOW,
    updated_at: NOW,
    reconciled_at: null,
    reconciled_balance_snapshot_id: null,
    editable: true,
    ...overrides,
  };
}

export const paymentPage: PaymentPage = {
  items: [
    payment(),
    payment({ id: 2, provider: "sarvam", amount: "2000", currency: "INR", payment_reference: "UPI-77" }),
    payment({ id: 3, provider: "deepgram", amount: "200.5", entry_method: "provider_verified", editable: false, payment_reference: null, external_id: "order-9" }),
  ],
  total: 3,
  limit: 25,
  offset: 0,
  totals: [
    { currency: "INR", net_amount: "2000", count: 1 },
    { currency: "USD", net_amount: "250.5", count: 2 },
  ],
};

export function alert(overrides: Partial<AlertEvent> = {}): AlertEvent {
  return {
    id: 10,
    provider: "deepgram",
    event_type: "breached",
    episode: 1,
    observed_amount: "18.4",
    threshold_amount: "25",
    unit: "USD",
    recipients: ["ops@example.com"],
    delivery_status: "sent",
    delivery_attempts: 1,
    last_error: null,
    subject: "[Low balance] Deepgram at 18.40 USD (threshold 25.00 USD)",
    dedupe_key: "t1:e1:breached",
    occurred_at: NOW,
    next_attempt_at: null,
    sent_at: NOW,
    source_type: "api_verified",
    attempts: [],
    ...overrides,
  };
}

export const alertPage: AlertPage = {
  items: [alert(), alert({ id: 11, delivery_status: "failed", delivery_attempts: 6, last_error: "unavailable: server down", sent_at: null })],
  total: 2,
  limit: 25,
  offset: 0,
};

export const overview: Overview = {
  generated_at: NOW,
  counts: { providers: 5, low_balance: 1, integration_failures: 1, not_configured: 1, stale_balances: 1 },
  last_successful_sync: NOW,
  providers: [
    {
      id: "deepgram",
      display_name: "Deepgram",
      integration_status: "ok",
      alert_state: "breached",
      evaluation_status: "breached",
      balance: "18.4",
      balance_unit: "USD",
      balance_source_type: "api_verified",
      balance_observed_at: NOW,
      balance_is_stale: false,
      threshold: "25",
      threshold_unit: "USD",
      headroom: { amount: "-6.6", unit: "USD", position: "below", balance_source_type: "api_verified", is_stale: false },
      last_successful_sync: NOW,
      failing_operations: ["usage"],
      last_error_code: "rate_limited",
      month_to_date_cost: [{ metric: "cost", unit: "USD", value: "12.5", dimension: "", observed_at: null, is_final: null }],
    },
    {
      id: "sarvam",
      display_name: "Sarvam AI",
      integration_status: "ok",
      alert_state: null,
      evaluation_status: null,
      balance: "2500",
      balance_unit: "INR",
      balance_source_type: "manual",
      balance_observed_at: "2026-10-10T06:00:00Z",
      balance_is_stale: true,
      threshold: null,
      threshold_unit: null,
      headroom: null,
      last_successful_sync: null,
      failing_operations: [],
      last_error_code: null,
      month_to_date_cost: [],
      month_to_date_usage: [],
      spend_reported: false,
    },
  ],
  aws_month_to_date: [{ metric: "cost_month", unit: "USD", value: "45.1", dimension: "", observed_at: NOW, is_final: false }],
  aws_month_forecast: [{ metric: "cost_forecast_month", unit: "USD", value: "115.1", dimension: "", observed_at: NOW, is_final: false }],
  recent_payments: paymentPage.items.slice(0, 2),
  recent_alerts: [alert()],
  spend_trend: [
    { metric: "cost", unit: "USD", dimension: "", provider: "deepgram", points: [{ period_start: "2026-10-12T00:00:00Z", period_end: "2026-10-13T00:00:00Z", value: "1.25", is_final: true }] },
    { metric: "cost", unit: "INR", dimension: "", provider: "sarvam", points: [{ period_start: "2026-10-12T00:00:00Z", period_end: "2026-10-13T00:00:00Z", value: "80", is_final: false }] },
  ],
  usage_trend: [],
};

export function threshold(overrides: Partial<Threshold> = {}): Threshold {
  return {
    provider: "deepgram",
    balance_kind: "prepaid_balance",
    amount: "25",
    unit: "USD",
    unit_type: "currency",
    is_enabled: true,
    notify_on_breach: true,
    notify_on_recovery: true,
    recovery_margin: "5",
    recovery_boundary: "30",
    cooldown_minutes: null,
    notification_emails: null,
    effective_recipients: ["ops@example.com"],
    eligible_sources: ["api_verified", "manual"],
    monitoring_interval_minutes: null,
    alert_state: "breached",
    breach_episode: 1,
    state_changed_at: NOW,
    last_evaluated_at: NOW,
    last_evaluation_status: "breached",
    last_evaluation_message: "18.40 USD <= threshold 25.00 USD",
    last_notified_at: NOW,
    email_delivery_enabled: false,
    version: 3,
    updated_at: NOW,
    ...overrides,
  };
}

export const syncStatus: ProviderSync = {
  provider: "deepgram",
  running: false,
  integration: providers[0]!.integration!,
  operations: [
    { operation: "balance", supported: true, status: "ok", last_attempt_at: NOW, last_successful_sync: NOW, seconds_since_success: 60, is_stale: false, consecutive_failures: 0, error_code: null, error_message: null },
    { operation: "usage", supported: true, status: "error", last_attempt_at: NOW, last_successful_sync: null, seconds_since_success: null, is_stale: true, consecutive_failures: 2, error_code: "rate_limited", error_message: "rate limited by provider (429)" },
  ],
};
