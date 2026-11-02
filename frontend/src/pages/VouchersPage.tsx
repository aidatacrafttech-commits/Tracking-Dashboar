import { useQueryClient } from "@tanstack/react-query";
import { useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/ui";
import { adminSecret, useAdminSecret } from "./vouchers/adminApi";
import { adminKeys } from "./vouchers/hooks";
import { PackagesPanel } from "./vouchers/PackagesPanel";
import { RedemptionsPanel } from "./vouchers/RedemptionsPanel";
import { SecretGate } from "./vouchers/SecretGate";
import { SubjectsPanel } from "./vouchers/SubjectsPanel";
import { VoucherList } from "./vouchers/VoucherList";

const TABS = [
  { id: "courses", label: "Course vouchers" },
  { id: "live-tutor", label: "Live Tutor vouchers" },
  { id: "redemptions", label: "Redemptions" },
  { id: "subjects", label: "Subjects" },
  { id: "packages", label: "Packages" },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** Voucher admin for the AdhyaapakAI backend (see docs/VOUCHERS.md). */
export function VouchersPage() {
  const { secret } = useAdminSecret();
  const client = useQueryClient();
  const signOut = () => {
    adminSecret.clear();
    client.removeQueries({ queryKey: adminKeys.all });
  };
  return (
    <>
      <PageHeader
        title="Voucher Management"
        description="Course and Live Tutor vouchers on the AdhyaapakAI backend. Dates are shown in IST."
        actions={secret ? <button type="button" className="btn" onClick={signOut}>Sign out of voucher admin</button> : undefined}
      />
      <SecretGate>
        <Workspace />
      </SecretGate>
    </>
  );
}

function Workspace() {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab: TabId = TABS.some((t) => t.id === requested) ? (requested as TabId) : "courses";
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const select = (id: TabId) => setParams({ tab: id }, { replace: true });
  // Arrow keys move between tabs (WAI-ARIA tabs pattern).
  const onKeyDown = (e: KeyboardEvent) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : -1;
    if (next < 0) return;
    e.preventDefault();
    const id = TABS[next]!.id;
    select(id);
    refs.current[id]?.focus();
  };
  return (
    <>
      <div className="tabs" role="tablist" aria-label="Voucher admin">
        {TABS.map((t) => (
          <button key={t.id} ref={(el) => { refs.current[t.id] = el; }} type="button" role="tab" id={`tab-${t.id}`}
            aria-selected={tab === t.id} aria-controls={`panel-${t.id}`} tabIndex={tab === t.id ? 0 : -1}
            className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => select(t.id)} onKeyDown={onKeyDown}>
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "courses" && <VoucherList key="course" category="course" />}
        {tab === "live-tutor" && <VoucherList key="live_tutor" category="live_tutor" />}
        {tab === "redemptions" && <RedemptionsPanel />}
        {tab === "subjects" && <SubjectsPanel />}
        {tab === "packages" && <PackagesPanel />}
      </div>
    </>
  );
}
