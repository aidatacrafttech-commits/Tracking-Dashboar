import type { ReactNode } from "react";
import { IconCopy } from "../../components/icons";
import { useToast } from "../../components/Toast";

/** A value with a copy button. The copied text goes only to the clipboard: it is never
 * logged or stored, and the confirmation names the field, not the value. */
export function Copyable({ value, label, children }: { value: string; label: string; children?: ReactNode }) {
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${label} copied.`);
    } catch {
      toast.error(null, `Could not copy the ${label.toLowerCase()}.`);
    }
  }
  return (
    <span className="copyable">
      <span>{children ?? value}</span>
      <button type="button" className="btn btn-ghost copy-btn" onClick={copy} aria-label={`Copy ${label.toLowerCase()} ${value}`} title={`Copy ${label.toLowerCase()}`}>
        <IconCopy size={14} />
      </button>
    </span>
  );
}
