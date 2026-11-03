import { useState, type ReactNode } from "react";

/** Drag-and-drop ordering (mouse) with Move up / Move down buttons (keyboard, touch,
 * screen readers). Calls onReorder with the full new id order. */
export function SortableList<T extends { id: number }>({
  items, label, render, onReorder, disabled,
}: {
  items: T[];
  label: (item: T) => string;
  render: (item: T) => ReactNode;
  onReorder: (ids: number[]) => void;
  disabled?: boolean;
}) {
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);

  const move = (from: number, to: number) => {
    if (to < 0 || to >= items.length || from === to) return;
    const ids = items.map((i) => i.id);
    const [id] = ids.splice(from, 1);
    ids.splice(to, 0, id!);
    onReorder(ids);
  };

  return (
    <ol className="sortable">
      {items.map((item, index) => (
        // Drag-and-drop is a mouse shortcut; the Move up / Move down buttons are the
        // keyboard and screen-reader equivalent.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <li
          key={item.id}
          className={`sortable-item ${dragging === item.id ? "dragging" : ""} ${over === item.id ? "drag-over" : ""}`}
          draggable={!disabled}
          onDragStart={(e) => { setDragging(item.id); e.dataTransfer.effectAllowed = "move"; }}
          onDragEnd={() => { setDragging(null); setOver(null); }}
          onDragOver={(e) => { if (dragging !== null) { e.preventDefault(); setOver(item.id); } }}
          onDrop={(e) => {
            e.preventDefault();
            const from = items.findIndex((i) => i.id === dragging);
            if (from >= 0) move(from, index);
            setDragging(null);
            setOver(null);
          }}
        >
          {!disabled && (
            <span className="sortable-controls">
              <span className="drag-handle" aria-hidden="true" title="Drag to reorder">⋮⋮</span>
              <button type="button" className="btn btn-sm btn-ghost" disabled={index === 0} onClick={() => move(index, index - 1)}
                aria-label={`Move ${label(item)} up`}>↑</button>
              <button type="button" className="btn btn-sm btn-ghost" disabled={index === items.length - 1} onClick={() => move(index, index + 1)}
                aria-label={`Move ${label(item)} down`}>↓</button>
            </span>
          )}
          <div className="sortable-body">{render(item)}</div>
        </li>
      ))}
    </ol>
  );
}
