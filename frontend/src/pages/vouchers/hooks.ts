import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Query } from "../../api/client";
import { adminFetch, useAdminSecret } from "./adminApi";
import type { Prefix, Redemption, Subject, TutorPackage, Voucher, VoucherList } from "./types";

// Every key starts with "adhyaapak", so one invalidation refreshes the voucher workspace.
export const adminKeys = {
  all: ["adhyaapak"] as const,
  vouchers: (prefix: Prefix, params: Query) => ["adhyaapak", "vouchers", prefix, params] as const,
  redemptions: (prefix: Prefix, id: string, params: Query) => ["adhyaapak", "redemptions", prefix, id, params] as const,
  subjects: ["adhyaapak", "subjects"] as const,
  packages: ["adhyaapak", "packages"] as const,
};

/** Queries run only while a secret is set (the gate shows the secret screen otherwise). */
function useEnabled() {
  return !!useAdminSecret().secret;
}

export const useAdminVouchers = (prefix: Prefix, params: Query) =>
  useQuery({
    queryKey: adminKeys.vouchers(prefix, params),
    queryFn: () => adminFetch<VoucherList>(`/vouchers/${prefix}`, { query: params }),
    placeholderData: keepPreviousData,
    enabled: useEnabled(),
    retry: false,
  });

export const useRedemptions = (prefix: Prefix, id: string, params: Query) =>
  useQuery({
    queryKey: adminKeys.redemptions(prefix, id, params),
    queryFn: () => adminFetch<{ redemptions: Redemption[]; total: number }>(`/vouchers/${prefix}/${id}/redemptions`, { query: params }),
    placeholderData: keepPreviousData,
    enabled: useEnabled(),
    retry: false,
  });

export const useSubjects = () =>
  useQuery({
    queryKey: adminKeys.subjects,
    queryFn: () => adminFetch<{ subjects: Subject[] }>("/vouchers/subjects"),
    enabled: useEnabled(),
    retry: false,
  });

export const usePackages = () =>
  useQuery({
    queryKey: adminKeys.packages,
    queryFn: () => adminFetch<{ packages: TutorPackage[] }>("/vouchers/live-tutor/packages/all"),
    enabled: useEnabled(),
    retry: false,
  });

function useAdminMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => client.invalidateQueries({ queryKey: adminKeys.all }) });
}

export const useCreateVoucher = (prefix: Prefix) =>
  useAdminMutation((body: Record<string, unknown>) =>
    adminFetch<{ voucher: Voucher }>(`/vouchers/${prefix}`, { method: "POST", body }));

export const useUpdateVoucher = (prefix: Prefix) =>
  useAdminMutation(({ id, body }: { id: string; body: Record<string, unknown> }) =>
    adminFetch<{ voucher: Voucher }>(`/vouchers/${prefix}/${id}`, { method: "PATCH", body }));

export const useSetVoucherEnabled = (prefix: Prefix) =>
  useAdminMutation(({ id, enabled }: { id: string; enabled: boolean }) =>
    adminFetch<{ voucher: Voucher }>(`/vouchers/${prefix}/${id}/${enabled ? "activate" : "deactivate"}`, { method: "POST" }));

export const useDeleteVoucher = (prefix: Prefix) =>
  useAdminMutation((id: string) => adminFetch<{ deleted: boolean; id: string }>(`/vouchers/${prefix}/${id}`, { method: "DELETE" }));

export const useCreateSubject = () =>
  useAdminMutation((body: Record<string, unknown>) => adminFetch<{ subject: Subject }>("/vouchers/subjects", { method: "POST", body }));

export const useUpdateSubject = () =>
  useAdminMutation(({ key, body }: { key: string; body: Record<string, unknown> }) =>
    adminFetch<{ subject: Subject }>(`/vouchers/subjects/${encodeURIComponent(key)}`, { method: "PATCH", body }));

export const useDeleteSubject = () =>
  useAdminMutation((key: string) => adminFetch<unknown>(`/vouchers/subjects/${encodeURIComponent(key)}`, { method: "DELETE" }));

export const useSavePackage = () =>
  useAdminMutation(({ key, body }: { key: string; body: Record<string, unknown> }) =>
    adminFetch<unknown>(`/vouchers/live-tutor/packages/${key}`, { method: "PUT", body }));

export interface RedemptionRow extends Redemption {
  voucher: Voucher;
}

const PAGE_SIZE = 50;
const MAX_PAGES = 40; // safety stop: 2,000 rows per list

async function allPages<T>(fetchPage: (skip: number) => Promise<{ items: T[]; total: number }>): Promise<T[]> {
  const out: T[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const { items, total } = await fetchPage(page * PAGE_SIZE);
    out.push(...items);
    if (items.length < PAGE_SIZE || out.length >= total) break;
  }
  return out;
}

/** Every redemption of every voucher (both categories, deleted ones included). The API only
 * lists redemptions per voucher, so this reads the voucher lists and then the redemptions of
 * each voucher that has been used, a few at a time. */
export const useAllRedemptions = () =>
  useQuery({
    queryKey: ["adhyaapak", "all-redemptions"],
    enabled: useEnabled(),
    retry: false,
    queryFn: async (): Promise<RedemptionRow[]> => {
      const vouchers = (
        await Promise.all(
          (["courses", "live-tutor"] as Prefix[]).map((prefix) =>
            allPages(async (skip) => {
              const r = await adminFetch<VoucherList>(`/vouchers/${prefix}`, { query: { include_deleted: true, limit: PAGE_SIZE, skip } });
              return { items: r.vouchers, total: r.total };
            }),
          ),
        )
      ).flat().filter((v) => v.stats.total_redemptions > 0);
      const rows: RedemptionRow[] = [];
      for (let i = 0; i < vouchers.length; i += 4) {
        const batch = await Promise.all(
          vouchers.slice(i, i + 4).map(async (voucher) => {
            const prefix: Prefix = voucher.category === "course" ? "courses" : "live-tutor";
            const list = await allPages(async (skip) => {
              const r = await adminFetch<{ redemptions: Redemption[]; total: number }>(
                `/vouchers/${prefix}/${voucher.id}/redemptions`, { query: { limit: PAGE_SIZE, skip } });
              return { items: r.redemptions, total: r.total };
            });
            return list.map((r) => ({ ...r, voucher }));
          }),
        );
        rows.push(...batch.flat());
      }
      const when = (r: Redemption) => Date.parse(r.completed_at ?? r.reserved_at ?? "") || 0;
      return rows.sort((a, b) => when(b) - when(a));
    },
  });
