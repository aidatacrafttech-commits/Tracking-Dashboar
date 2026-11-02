import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { config } from "../config";
import { apiFetch, type Query } from "./client";
import type {
  AlertEventDetail,
  AlertPage,
  AppSettings,
  BalanceSnapshot,
  EmailPreview,
  EvaluationResult,
  HealthCheck,
  NotificationStatus,
  Overview,
  Payment,
  PaymentInput,
  PaymentPage,
  Provider,
  ProviderSync,
  ReconcilePreview,
  ResourceList,
  SyncRun,
  TestEmailResult,
  Threshold,
  ThresholdInput,
  UsageSeries,
} from "./types";

/** Query keys grouped by the backend change-stream topic that invalidates them. */
export const keys = {
  overview: ["overview"] as const,
  settings: ["settings"] as const,
  providers: ["providers"] as const,
  provider: (id: string) => ["providers", id] as const,
  sync: (id: string) => ["sync", id] as const,
  usage: (id: string, params: Query) => ["usage", id, params] as const,
  balances: (id: string, params: Query) => ["balances", id, params] as const,
  payments: (params: Query) => ["payments", params] as const,
  reconcile: (id: number) => ["payments", "reconcile", id] as const,
  threshold: (id: string) => ["thresholds", id] as const,
  alerts: (params: Query) => ["alerts", params] as const,
  alert: (id: number) => ["alerts", "detail", id] as const,
  health: (id: string) => ["health", id] as const,
  resources: ["resources"] as const,
  notifications: ["notifications"] as const,
};

/** Backend change topics -> query key prefixes to refetch (see services/changes.py). */
export const TOPIC_KEYS: Record<string, readonly (readonly unknown[])[]> = {
  balances: [["overview"], ["providers"], ["balances"], ["thresholds"]],
  usage: [["overview"], ["usage"]],
  sync: [["overview"], ["providers"], ["sync"]],
  alerts: [["overview"], ["alerts"], ["thresholds"], ["notifications"]],
  payments: [["overview"], ["payments"], ["balances"], ["providers"]],
  thresholds: [["overview"], ["providers"], ["thresholds"]],
  resources: [["resources"]],
  health: [["overview"], ["health"]],
};

const live = { refetchInterval: config.pollIntervalMs, refetchIntervalInBackground: false } as const;

export const useAppSettings = () =>
  useQuery({ queryKey: keys.settings, queryFn: () => apiFetch<AppSettings>("/settings"), staleTime: 300_000 });

export const useOverview = () =>
  useQuery({ queryKey: keys.overview, queryFn: () => apiFetch<Overview>("/overview"), ...live });

export const useProviders = () =>
  useQuery({ queryKey: keys.providers, queryFn: () => apiFetch<Provider[]>("/providers"), ...live });

export const useProvider = (id: string) =>
  useQuery({ queryKey: keys.provider(id), queryFn: () => apiFetch<Provider>(`/providers/${id}`), ...live });

export const useSyncStatus = (id: string) =>
  useQuery({ queryKey: keys.sync(id), queryFn: () => apiFetch<ProviderSync>(`/providers/${id}/sync-status`), ...live });

export const useUsageSeries = (id: string, params: { days: number; metric?: string; by_dimension?: boolean }) =>
  useQuery({
    queryKey: keys.usage(id, params),
    queryFn: () => apiFetch<UsageSeries>(`/providers/${id}/usage-series`, { query: params }),
    ...live,
  });

export const useBalanceHistory = (id: string, days: number) =>
  useQuery({
    queryKey: keys.balances(id, { days }),
    queryFn: () =>
      apiFetch<{ items: BalanceSnapshot[] }>(`/providers/${id}/snapshots`, {
        query: { kind: "balance", limit: 500, start: new Date(Date.now() - days * 86_400_000).toISOString() },
      }),
    ...live,
  });

export const usePayments = (params: Query) =>
  useQuery({
    queryKey: keys.payments(params),
    queryFn: () => apiFetch<PaymentPage>("/payments", { query: params }),
    placeholderData: keepPreviousData,
    ...live,
  });

export const useThreshold = (id: string) =>
  useQuery({
    queryKey: keys.threshold(id),
    queryFn: () =>
      apiFetch<Threshold>(`/providers/${id}/threshold`).catch((err) => {
        if (err?.code === "threshold_not_configured") return null;
        throw err;
      }),
  });

export const useAlerts = (params: Query) =>
  useQuery({
    queryKey: keys.alerts(params),
    queryFn: () => apiFetch<AlertPage>("/alerts", { query: params }),
    placeholderData: keepPreviousData,
    ...live,
  });

export const useAlertDetail = (id: number | null) =>
  useQuery({
    queryKey: keys.alert(id ?? 0),
    queryFn: () => apiFetch<AlertEventDetail>(`/alerts/${id}`),
    enabled: id !== null,
  });

export const useHealthChecks = (id: string) =>
  useQuery({
    queryKey: keys.health(id),
    queryFn: () => apiFetch<HealthCheck[]>(`/providers/${id}/health-checks`, { query: { limit: 20 } }),
    ...live,
  });

export const useResources = () =>
  useQuery({ queryKey: keys.resources, queryFn: () => apiFetch<ResourceList>("/aws/resources"), ...live });

export const useNotificationStatus = () =>
  useQuery({ queryKey: keys.notifications, queryFn: () => apiFetch<NotificationStatus>("/notifications/status") });

export const useReconcilePreview = (paymentId: number | null) =>
  useQuery({
    queryKey: keys.reconcile(paymentId ?? 0),
    queryFn: () => apiFetch<ReconcilePreview>(`/payments/${paymentId}/reconcile-preview`),
    enabled: paymentId !== null,
    staleTime: 0,
  });

// --- mutations: each invalidates what it changed ------------------------------------------

function useInvalidate() {
  const qc = useQueryClient();
  return (...prefixes: (readonly unknown[])[]) =>
    Promise.all(prefixes.map((queryKey) => qc.invalidateQueries({ queryKey })));
}

export function useCreatePayment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ provider, body }: { provider: string; body: PaymentInput }) =>
      apiFetch<Payment>(`/providers/${provider}/payments`, { method: "POST", body }),
    onSuccess: () => invalidate(["payments"], ["overview"]),
  });
}

export function useUpdatePayment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: PaymentInput }) =>
      apiFetch<Payment>(`/payments/${id}`, { method: "PUT", body }),
    onSuccess: () => invalidate(["payments"], ["overview"]),
  });
}

export function useDeletePayment() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: number) => apiFetch<void>(`/payments/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidate(["payments"], ["overview"]),
  });
}

export function useReconcile() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, baseSnapshotId }: { id: number; baseSnapshotId: number }) =>
      apiFetch<BalanceSnapshot>(`/payments/${id}/reconcile`, {
        method: "POST",
        body: { base_snapshot_id: baseSnapshotId },
      }),
    onSuccess: () => invalidate(["payments"], ["overview"], ["providers"], ["balances"], ["thresholds"]),
  });
}

export function useSaveThreshold(id: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: ThresholdInput) => apiFetch<Threshold>(`/providers/${id}/threshold`, { method: "PUT", body }),
    onSuccess: () => invalidate(["thresholds"], ["providers"], ["overview"], ["alerts"]),
  });
}

export function useEvaluateThreshold(id: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: () => apiFetch<EvaluationResult>(`/providers/${id}/threshold/evaluate`, { method: "POST" }),
    onSuccess: () => invalidate(["thresholds"], ["providers"], ["overview"], ["alerts"]),
  });
}

export function usePreviewEmail() {
  return useMutation({
    mutationFn: ({ provider, eventType }: { provider: string; eventType: string }) =>
      apiFetch<EmailPreview>(`/providers/${provider}/alert-preview`, { query: { event_type: eventType } }),
  });
}

export function useTestEmail() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (to: string | null) =>
      apiFetch<TestEmailResult>("/notifications/test-email", { method: "POST", body: to ? { to } : {} }),
    onSuccess: () => invalidate(["alerts"], ["notifications"]),
  });
}

export function useRetryAlert() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: number) => apiFetch<AlertEventDetail>(`/alerts/${id}/retry`, { method: "POST" }),
    onSuccess: () => invalidate(["alerts"], ["overview"]),
  });
}

export function useSyncNow(id: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: () => apiFetch<SyncRun>(`/providers/${id}/sync`, { method: "POST" }),
    onSettled: () => invalidate(["sync"], ["providers"], ["overview"], ["usage"], ["balances"], ["health"]),
  });
}

export function useManualBalance(id: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: { amount: string; unit: string; observed_at?: string; notes?: string; balance_kind?: string }) =>
      apiFetch<BalanceSnapshot>(`/providers/${id}/balance-entries`, { method: "POST", body }),
    onSuccess: () => invalidate(["providers"], ["overview"], ["balances"], ["thresholds"], ["alerts"]),
  });
}

export interface OpsStatus {
  status: "ok" | "warning" | "critical";
  problems: string[];
  disk_free_mb: number;
  scheduler: string;
}

/** Owner-only operational health (backups, disk, scheduler, failed emails). */
export const useOpsStatus = (enabled: boolean) =>
  useQuery({
    queryKey: ["ops"],
    queryFn: () => apiFetch<OpsStatus>("/admin/operations"),
    enabled,
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
