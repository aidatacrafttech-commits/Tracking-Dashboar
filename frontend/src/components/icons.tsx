// Small inline icon set (no icon-font dependency). Decorative by default (aria-hidden).

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 16, ...props }: IconProps, path: React.ReactNode) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {path}
    </svg>
  );
}

export const IconOverview = (p: IconProps) => base(p, <><rect x="3" y="3" width="7" height="9" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="12" width="7" height="9" /><rect x="3" y="16" width="7" height="5" /></>);
export const IconProviders = (p: IconProps) => base(p, <><path d="M4 7h16M4 12h16M4 17h10" /></>);
export const IconPayments = (p: IconProps) => base(p, <><rect x="2" y="5" width="20" height="14" rx="2" /><path d="M2 10h20" /></>);
export const IconThresholds = (p: IconProps) => base(p, <><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" /></>);
export const IconAlerts = (p: IconProps) => base(p, <><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></>);
export const IconTicket = (p: IconProps) => base(p, <><path d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v3a2 2 0 0 0 0 4v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 0 0-4z" /><path d="M13 5v2M13 11v2M13 17v2" /></>);
export const IconCloud = (p: IconProps) => base(p, <><path d="M18 10h-1.3A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" /></>);
export const IconCheck = (p: IconProps) => base(p, <path d="M20 6 9 17l-5-5" />);
export const IconWarning = (p: IconProps) => base(p, <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4M12 17h.01" /></>);
export const IconCritical = (p: IconProps) => base(p, <><circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" /></>);
export const IconMinus = (p: IconProps) => base(p, <><circle cx="12" cy="12" r="10" /><path d="M8 12h8" /></>);
export const IconInfo = (p: IconProps) => base(p, <><circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" /></>);
export const IconRefresh = (p: IconProps) => base(p, <><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></>);
export const IconPlus = (p: IconProps) => base(p, <path d="M12 5v14M5 12h14" />);
export const IconMenu = (p: IconProps) => base(p, <path d="M3 6h18M3 12h18M3 18h18" />);
export const IconClose = (p: IconProps) => base(p, <path d="M18 6 6 18M6 6l12 12" />);
export const IconCopy = (p: IconProps) => base(p, <><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" /></>);
export const IconClock = (p: IconProps) => base(p, <><circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" /></>);
export const IconSort = (p: IconProps & { direction?: "ascending" | "descending" | "none" }) => {
  const { direction = "none", ...rest } = p;
  return base(rest, direction === "ascending" ? <path d="m6 15 6-6 6 6" /> : direction === "descending" ? <path d="m6 9 6 6 6-6" /> : <path d="m7 9 5-5 5 5M7 15l5 5 5-5" />);
};
export const IconLogo = (p: IconProps) => (
  <svg width={p.size ?? 26} height={p.size ?? 26} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
    <rect width="32" height="32" rx="7" fill="#2563eb" />
    <path d="M8 21l5-6 4 4 7-9" fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
