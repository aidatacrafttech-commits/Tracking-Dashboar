import { useEffect, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useEvaluateThreshold,
  useNotificationStatus,
  usePreviewEmail,
  useProviders,
  useSaveThreshold,
  useTestEmail,
  useThreshold,
} from "../api/hooks";
import type { EligibleSource, EmailPreview, Provider, Threshold, ThresholdInput } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Dialog } from "../components/Dialog";
import { useToast } from "../components/Toast";
import { Badge, Card, ErrorState, LoadingBlock, Notice, PageHeader, StatusBadge } from "../components/ui";
import { formatAmount, isCurrency, titleCase } from "../lib/format";
import { alertStatus } from "../lib/status";

const EMAIL_RE = /^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;
const SOURCES: { value: EligibleSource; label: string; hint: string }[] = [
  { value: "api_verified", label: "Verified by provider API", hint: "Balances the provider reports itself" },
  { value: "manual", label: "Entered manually", hint: "Balances you type in from the provider dashboard" },
  { value: "usage_estimate", label: "Estimated from usage", hint: "Last manual balance minus reported costs (conservative)" },
];

interface FormState {
  amount: string;
  is_enabled: boolean;
  notify_on_breach: boolean;
  notify_on_recovery: boolean;
  recovery_margin: string;
  cooldown_minutes: string;
  monitoring_interval_minutes: string;
  recipients: string;
  eligible_sources: EligibleSource[];
}

export function validateThreshold(form: FormState, unit: string): Record<string, string> {
  const errors: Record<string, string> = {};
  const money = isCurrency(unit);
  const decimal = money ? /^\d+(\.\d{1,2})?$/ : /^\d+$/;
  if (!decimal.test(form.amount.trim())) errors.amount = money ? "Enter an amount like 25.00." : "Enter a whole number of credits.";
  if (!decimal.test(form.recovery_margin.trim() || "0")) errors.recovery_margin = money ? "Enter an amount like 5.00, or 0." : "Enter a whole number, or 0.";
  const cooldown = form.cooldown_minutes.trim();
  if (cooldown && (!/^\d+$/.test(cooldown) || +cooldown < 15 || +cooldown > 10080)) errors.cooldown_minutes = "Between 15 and 10080 minutes, or empty for one email per breach.";
  const interval = form.monitoring_interval_minutes.trim();
  if (interval && (!/^\d+$/.test(interval) || +interval < 5 || +interval > 1440)) errors.monitoring_interval_minutes = "Between 5 and 1440 minutes, or empty for the default.";
  const emails = form.recipients.split(",").map((e) => e.trim()).filter(Boolean);
  const bad = emails.find((e) => !EMAIL_RE.test(e));
  if (bad) errors.notification_emails = `"${bad}" is not a valid email address.`;
  if (emails.length > 10) errors.notification_emails = "At most 10 recipients.";
  if (form.eligible_sources.length === 0) errors.eligible_sources = "Choose at least one balance source.";
  return errors;
}

function toInput(form: FormState, unit: string): ThresholdInput {
  const emails = form.recipients.split(",").map((e) => e.trim()).filter(Boolean);
  return {
    amount: form.amount.trim(),
    unit,
    is_enabled: form.is_enabled,
    notify_on_breach: form.notify_on_breach,
    notify_on_recovery: form.notify_on_recovery,
    recovery_margin: form.recovery_margin.trim() || "0",
    cooldown_minutes: form.cooldown_minutes.trim() ? Number(form.cooldown_minutes) : null,
    monitoring_interval_minutes: form.monitoring_interval_minutes.trim() ? Number(form.monitoring_interval_minutes) : null,
    notification_emails: emails.length ? emails : null,
    eligible_sources: form.eligible_sources,
  };
}

function initialThresholdForm(threshold: Threshold | null | undefined, provider: Provider): FormState {
  return {
    amount: threshold?.amount ?? "",
    is_enabled: threshold?.is_enabled ?? true,
    notify_on_breach: threshold?.notify_on_breach ?? true,
    notify_on_recovery: threshold?.notify_on_recovery ?? true,
    recovery_margin: threshold?.recovery_margin ?? "0",
    cooldown_minutes: threshold?.cooldown_minutes?.toString() ?? "",
    monitoring_interval_minutes: threshold?.monitoring_interval_minutes?.toString() ?? "",
    recipients: threshold?.notification_emails?.join(", ") ?? "",
    eligible_sources:
      threshold?.eligible_sources ?? (provider.balance_capability === "manual_only" ? ["manual"] : ["api_verified", "manual"]),
  };
}

function ThresholdForm({ provider, onPreview }: { provider: Provider; onPreview: (p: EmailPreview) => void }) {
  const { data: threshold, isPending, error, refetch } = useThreshold(provider.id);
  if (isPending) return <LoadingBlock rows={4} />;
  if (error) return <ErrorState error={error} onRetry={() => refetch()} />;
  // Re-initialise the form whenever the saved configuration changes (new version).
  return <ThresholdFormInner key={threshold?.version ?? "new"} provider={provider} threshold={threshold} onPreview={onPreview} />;
}

function ThresholdFormInner({
  provider,
  threshold,
  onPreview,
}: {
  provider: Provider;
  threshold: Threshold | null;
  onPreview: (p: EmailPreview) => void;
}) {
  const toast = useToast();
  const { canWrite } = useAuth();
  const save = useSaveThreshold(provider.id);
  const evaluate = useEvaluateThreshold(provider.id);
  const preview = usePreviewEmail();
  const [form, setForm] = useState<FormState>(() => initialThresholdForm(threshold, provider));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const unit = provider.default_unit;
  const id = (name: string) => `${provider.id}-${name}`;
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm({ ...form, [key]: value });

  async function submit(e: FormEvent) {
    e.preventDefault();
    const found = validateThreshold(form, unit);
    setErrors(found);
    if (Object.keys(found).length) return;
    try {
      const saved = await save.mutateAsync(toInput(form, unit));
      toast.success(`${provider.display_name} threshold saved (${formatAmount(saved.amount, saved.unit)}). Current status: ${alertStatus(saved.alert_state, saved.last_evaluation_status).label}.`);
    } catch (err) {
      if (err instanceof ApiError) {
        const fields = err.fieldErrors();
        setErrors(Object.keys(fields).length ? fields : { form: err.message });
      } else setErrors({ form: "Could not save." });
    }
  }

  const describe = (name: string, hint?: string) => [hint ? id(`${name}-hint`) : null, errors[name] ? id(`${name}-error`) : null].filter(Boolean).join(" ") || undefined;
  const err = (name: string) => errors[name] && <span id={id(`${name}-error`)} className="error" role="alert">{errors[name]}</span>;

  return (
    <form onSubmit={submit} noValidate className="stack" style={{ gap: 14 }}>
      {threshold && (
        <div className="row">
          <StatusBadge status={alertStatus(threshold.alert_state, threshold.last_evaluation_status)} />
          {threshold.last_evaluation_message && <span className="small muted">{threshold.last_evaluation_message}</span>}
        </div>
      )}
      {errors.form && <Notice tone="critical">{errors.form}</Notice>}
      <fieldset disabled={!canWrite} style={{ border: 0, padding: 0 }} className="stack">
        <div className="form-grid">
          <div className="field">
            <label htmlFor={id("amount")}>Alert when balance is at or below ({unit})</label>
            <input id={id("amount")} type="text" inputMode="decimal" value={form.amount} onChange={(e) => set("amount", e.target.value)} aria-invalid={!!errors.amount} aria-describedby={describe("amount")} />
            {err("amount")}
          </div>
          <div className="field">
            <label htmlFor={id("margin")}>Recovery margin ({unit})</label>
            <input id={id("margin")} type="text" inputMode="decimal" value={form.recovery_margin} onChange={(e) => set("recovery_margin", e.target.value)} aria-invalid={!!errors.recovery_margin} aria-describedby={describe("recovery_margin", "h")} />
            <span className="hint" id={id("recovery_margin-hint")}>Recovered only above threshold + margin</span>
            {err("recovery_margin")}
          </div>
          <div className="field">
            <label htmlFor={id("cooldown")}>Repeat reminder every (minutes)</label>
            <input id={id("cooldown")} type="number" min={15} max={10080} value={form.cooldown_minutes} onChange={(e) => set("cooldown_minutes", e.target.value)} aria-invalid={!!errors.cooldown_minutes} aria-describedby={describe("cooldown_minutes", "h")} />
            <span className="hint" id={id("cooldown_minutes-hint")}>Empty: one email per breach</span>
            {err("cooldown_minutes")}
          </div>
          <div className="field">
            <label htmlFor={id("interval")}>Check balance every (minutes)</label>
            <input id={id("interval")} type="number" min={5} max={1440} value={form.monitoring_interval_minutes} onChange={(e) => set("monitoring_interval_minutes", e.target.value)} aria-invalid={!!errors.monitoring_interval_minutes} aria-describedby={describe("monitoring_interval_minutes", "h")} />
            <span className="hint" id={id("monitoring_interval_minutes-hint")}>Empty: server default</span>
            {err("monitoring_interval_minutes")}
          </div>
        </div>
        <div className="field">
          <label htmlFor={id("recipients")}>Alert recipients</label>
          <input id={id("recipients")} type="text" placeholder={threshold?.effective_recipients.join(", ") || "ops@example.com"} value={form.recipients} onChange={(e) => set("recipients", e.target.value)} aria-invalid={!!errors.notification_emails} aria-describedby={describe("notification_emails", "h")} />
          <span className="hint" id={id("notification_emails-hint")}>Comma-separated. Empty: the server&apos;s ALERT_RECIPIENTS.</span>
          {err("notification_emails")}
        </div>
        <fieldset>
          <legend>Balance sources that may trigger alerts</legend>
          <div className="stack" style={{ gap: 6 }}>
            {SOURCES.map((s) => (
              <label key={s.value} className="checkbox">
                <input
                  type="checkbox"
                  checked={form.eligible_sources.includes(s.value)}
                  onChange={(e) => set("eligible_sources", e.target.checked ? [...form.eligible_sources, s.value] : form.eligible_sources.filter((v) => v !== s.value))}
                />
                <span>
                  {s.label} <span className="small muted">— {s.hint}</span>
                </span>
              </label>
            ))}
          </div>
          {errors.eligible_sources && <span className="error" role="alert">{errors.eligible_sources}</span>}
        </fieldset>
        <div className="row">
          <label className="checkbox"><input type="checkbox" checked={form.is_enabled} onChange={(e) => set("is_enabled", e.target.checked)} />Threshold enabled</label>
          <label className="checkbox"><input type="checkbox" checked={form.notify_on_breach} onChange={(e) => set("notify_on_breach", e.target.checked)} />Email on low balance</label>
          <label className="checkbox"><input type="checkbox" checked={form.notify_on_recovery} onChange={(e) => set("notify_on_recovery", e.target.checked)} />Email on recovery</label>
        </div>
      </fieldset>
      <div className="row">
        {canWrite && (
          <button type="submit" className="btn btn-primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save threshold"}
          </button>
        )}
        {threshold && (
          <button
            type="button"
            className="btn"
            disabled={preview.isPending}
            onClick={async () => {
              try {
                onPreview(await preview.mutateAsync({ provider: provider.id, eventType: "breached" }));
              } catch (e) {
                toast.error(e);
              }
            }}
          >
            Preview alert email
          </button>
        )}
        {threshold && canWrite && (
          <button
            type="button"
            className="btn"
            disabled={evaluate.isPending}
            onClick={async () => {
              try {
                const r = await evaluate.mutateAsync();
                toast.info(`${provider.display_name}: ${r.message}${r.events_created.length ? " — alert created" : ""}`);
              } catch (e) {
                toast.error(e);
              }
            }}
          >
            Check now
          </button>
        )}
      </div>
    </form>
  );
}

function EmailDelivery() {
  const toast = useToast();
  const { canWrite } = useAuth();
  const { data, isPending, error, refetch } = useNotificationStatus();
  const test = useTestEmail();
  const [to, setTo] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);

  async function sendTest(e: FormEvent) {
    e.preventDefault();
    if (to.trim() && !EMAIL_RE.test(to.trim())) {
      setFieldError("Enter a valid email address, or leave empty for the default recipients.");
      return;
    }
    setFieldError(null);
    try {
      const r = await test.mutateAsync(to.trim() || null);
      if (r.delivery_status === "sent") toast.success(`Test email sent to ${r.recipients.join(", ")}.`);
      else toast.error(null, `Test email ${r.delivery_status}: ${r.error_message ?? r.error_code}`);
    } catch (err) {
      toast.error(err);
    }
  }

  return (
    <Card title="Email delivery" description="Server SMTP configuration (credentials are never shown)">
      {isPending ? (
        <LoadingBlock rows={3} />
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        <div className="stack">
          {!data.email_delivery_enabled && (
            <Notice tone="warning">
              Alert emails are <strong>off</strong> (<code>EMAIL_DELIVERY_ENABLED=false</code>). Alerts are still evaluated and listed in Alert
              history, marked &ldquo;Not emailed&rdquo;. The test email below still works once SMTP is configured.
            </Notice>
          )}
          <dl className="kv small">
            <dt>SMTP</dt>
            <dd>{data.smtp_configured ? <Badge tone="normal">{`${data.smtp_host}:${data.smtp_port} (${data.smtp_security})`}</Badge> : <Badge tone="unavailable">Not configured</Badge>}</dd>
            <dt>Sender</dt>
            <dd>{data.smtp_from ?? "—"}</dd>
            <dt>Default recipients</dt>
            <dd>{data.default_recipients.join(", ") || "None (set ALERT_RECIPIENTS)"}</dd>
            <dt>Outbox</dt>
            <dd>
              {Object.entries(data.outbox).filter(([, n]) => n > 0).map(([s, n]) => `${n} ${titleCase(s).toLowerCase()}`).join(", ") || "Empty"}
            </dd>
          </dl>
          {canWrite && (
            <form onSubmit={sendTest} noValidate className="row" style={{ alignItems: "flex-end" }}>
              <div className="field" style={{ flex: "1 1 220px" }}>
                <label htmlFor="test-to">Send a test email to</label>
                <input id="test-to" type="email" placeholder={data.default_recipients[0] ?? "you@example.com"} value={to} onChange={(e) => setTo(e.target.value)} aria-invalid={!!fieldError} aria-describedby={fieldError ? "test-to-error" : undefined} />
                {fieldError && <span id="test-to-error" className="error" role="alert">{fieldError}</span>}
              </div>
              <button type="submit" className="btn" disabled={!data.smtp_configured || test.isPending}>
                {test.isPending ? "Sending…" : "Send test email"}
              </button>
            </form>
          )}
        </div>
      )}
    </Card>
  );
}

export function ThresholdsPage() {
  const { data: providers, isPending, error, refetch } = useProviders();
  const [preview, setPreview] = useState<EmailPreview | null>(null);
  const location = useLocation();

  useEffect(() => {
    if (!providers || !location.hash) return;
    document.getElementById(`threshold-${location.hash.slice(1)}`)?.scrollIntoView({ block: "start" });
  }, [providers, location.hash]);

  return (
    <>
      <PageHeader title="Thresholds" description="Low-balance alert settings per provider. Changes apply immediately." />
      <EmailDelivery />
      {isPending ? (
        <div className="card"><LoadingBlock rows={6} /></div>
      ) : error ? (
        <ErrorState error={error} onRetry={() => refetch()} />
      ) : (
        providers.map((p) => (
          <section key={p.id} id={`threshold-${p.id}`} className="card" aria-labelledby={`threshold-${p.id}-title`}>
            <div className="card-header">
              <div>
                <h2 id={`threshold-${p.id}-title`}>{p.display_name}</h2>
                <p>
                  Watches {p.balance_capability === "postpaid" ? "remaining promotional credits" : p.default_unit_type === "credits" ? "plan credits (not money)" : "prepaid balance"} in{" "}
                  {p.default_unit}
                  {p.balance_capability === "manual_only" && " — no balance API, so enable “Entered manually” and/or “Estimated from usage”"}
                </p>
              </div>
            </div>
            <ThresholdForm provider={p} onPreview={setPreview} />
          </section>
        ))
      )}
      <Dialog open={!!preview} onClose={() => setPreview(null)} title="Alert email preview" description={preview?.note} wide>
        {preview && (
          <>
            <p>
              <strong>Subject:</strong> {preview.subject}
            </p>
            <p className="small muted">Would go to: {preview.recipients.join(", ") || "no recipients configured"}</p>
            {/* Sandboxed: the email HTML cannot run scripts or reach this page. */}
            <iframe title="Rendered alert email" className="email-frame" sandbox="" srcDoc={preview.html} />
          </>
        )}
      </Dialog>
    </>
  );
}
