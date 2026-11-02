import { useEffect, useId, useRef, type ReactNode } from "react";
import { IconClose } from "./icons";

/**
 * Modal built on the native <dialog> element: focus moves into the dialog, Tab stays
 * inside it, Escape closes it and focus returns to the opener. No custom focus trap.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide = false,
  drawer = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  /** Slides in from the right edge, full height (for long side panels). */
  drawer?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", ""); // jsdom
      // Prefer an element marked as the safe initial focus (e.g. "Cancel").
      dialog.querySelector<HTMLElement>("[data-initial-focus]")?.focus();
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={drawer ? "drawer" : wide ? "wide" : undefined}
      aria-labelledby={titleId}
      aria-describedby={description ? descId : undefined}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      {open && (
        <>
          <div className="dialog-header">
            <div className="stack" style={{ gap: 4 }}>
              <h2 id={titleId}>{title}</h2>
              {description && (
                <p id={descId} className="muted">
                  {description}
                </p>
              )}
            </div>
            <button type="button" className="btn btn-ghost icon-btn" onClick={onClose} aria-label="Close dialog">
              <IconClose />
            </button>
          </div>
          <div className="dialog-body">{children}</div>
          {footer && <div className="dialog-footer">{footer}</div>}
        </>
      )}
    </dialog>
  );
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  destructive = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} data-initial-focus>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${destructive ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}
