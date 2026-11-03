import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatDayLabel } from "../../lib/format";
import type { DayRow } from "./api";

// Days arrive as calendar dates in the display timezone (not UTC instants), so they are
// labelled as plain dates; TrendChart would bucket and label them in UTC.
const SERIES = [
  { key: "signups", name: "Signups", color: "var(--chart-1)" },
  { key: "deleted_since", name: "Signed up, deleted since", color: "var(--chart-4)" },
] as const;

const dayLabel = (date: string) => formatDayLabel(`${date}T00:00:00Z`);

export function DailyBars({ title, rows, onSelectDay, height = 240 }: {
  title: string;
  rows: DayRow[];
  onSelectDay?: (date: string) => void;
  height?: number;
}) {
  const data = rows.map((r) => ({ ...r, label: dayLabel(r.date) }));
  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="visually-hidden">{title}</figcaption>
      <div className="chart-box" style={{ height }} role="img" aria-label={`${title}. Exact values are in the data table below.`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--text-3)" }} tickLine={false} axisLine={false} minTickGap={16} />
            <YAxis tick={{ fontSize: 11, fill: "var(--text-3)" }} tickLine={false} axisLine={false} width={40} allowDecimals={false} />
            <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12.5 }} />
            {SERIES.map((s) => (
              <Bar key={s.key} dataKey={s.key} name={s.name} stackId="a" fill={s.color} isAnimationActive={false}
                cursor={onSelectDay ? "pointer" : undefined}
                onClick={onSelectDay ? (bar) => onSelectDay((bar as unknown as { payload: DayRow }).payload.date) : undefined} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="chart-legend">
        {SERIES.map((s) => (
          <span key={s.key}>
            <span className="legend-swatch" style={{ background: s.color }} aria-hidden="true" />
            {s.name}
          </span>
        ))}
      </div>
      <details className="chart-data">
        <summary>Show data table</summary>
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region must be keyboard reachable */}
        <div className="table-wrap" style={{ marginTop: 8, maxHeight: 260, overflow: "auto" }} tabIndex={0} role="region" aria-label={`${title} data`}>
          <table>
            <caption className="visually-hidden">{title} data</caption>
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col" className="num">Signups</th>
                <th scope="col" className="num">Deleted since</th>
                <th scope="col" className="num">Paid purchases</th>
                <th scope="col" className="num">Vouchers</th>
              </tr>
            </thead>
            <tbody>
              {[...rows].reverse().map((r) => (
                <tr key={r.date}>
                  <td>
                    {onSelectDay ? (
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => onSelectDay(r.date)} aria-label={`Show signups on ${r.date}`}>{r.date}</button>
                    ) : r.date}
                  </td>
                  <td className="num">{r.signups}</td>
                  <td className="num">{r.deleted_since}</td>
                  <td className="num">{r.purchases}</td>
                  <td className="num">{r.vouchers}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
