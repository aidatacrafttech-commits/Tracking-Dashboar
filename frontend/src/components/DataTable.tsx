import type { ReactNode } from "react";
import { IconSort } from "./icons";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  numeric?: boolean;
  sortable?: boolean;
}

export interface SortState {
  key: string;
  order: "asc" | "desc";
}

/**
 * Accessible table: real <th scope="col">, aria-sort on sortable headers driven by
 * buttons (keyboard operable). Collapses into labelled cards on narrow screens.
 */
export function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  sort,
  onSort,
  empty,
}: {
  caption: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  sort?: SortState;
  onSort?: (key: string) => void;
  empty?: ReactNode;
}) {
  if (rows.length === 0 && empty) return <>{empty}</>;
  return (
    // Focusable, labelled region so keyboard users can scroll wide tables sideways.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div className="table-wrap" tabIndex={0} role="region" aria-label={`${caption} table`}>
      <table className="responsive">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {columns.map((col) => {
              const active = sort?.key === col.key;
              const ariaSort = active ? (sort!.order === "asc" ? "ascending" : "descending") : "none";
              return (
                <th
                  key={col.key}
                  scope="col"
                  className={col.numeric ? "num" : undefined}
                  aria-sort={col.sortable ? ariaSort : undefined}
                >
                  {col.sortable && onSort ? (
                    <button type="button" className="sort-btn" onClick={() => onSort(col.key)}>
                      {col.header}
                      <IconSort size={12} direction={ariaSort} />
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((col) => (
                <td key={col.key} data-label={col.header} className={col.numeric ? "num" : undefined}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Pagination({
  total,
  limit,
  offset,
  onChange,
  label = "results",
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
  label?: string;
}) {
  if (total <= limit) return total ? <div className="pagination small">{total} {label}</div> : null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <nav className="pagination" aria-label="Pagination">
      <span className="small">
        {offset + 1}–{Math.min(offset + limit, total)} of {total} {label}
      </span>
      <div className="row">
        <button type="button" className="btn btn-sm" disabled={page <= 1} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </button>
        <span className="small" aria-live="polite">
          Page {page} of {pages}
        </span>
        <button type="button" className="btn btn-sm" disabled={page >= pages} onClick={() => onChange(offset + limit)}>
          Next
        </button>
      </div>
    </nav>
  );
}
