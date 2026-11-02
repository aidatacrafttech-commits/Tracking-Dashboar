import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Series } from "../api/types";
import { formatAmount, formatAxis, formatDayLabel } from "../lib/format";

const COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "var(--chart-6)"];

interface Row {
  day: string;
  label: string;
  [series: string]: string | number | null;
}

/**
 * Merges series that share ONE unit into chart rows. Values are converted to numbers only
 * for drawing; tooltips and the data table show the exact decimal strings from the API.
 */
function toRows(series: Series[], labelOf: (s: Series) => string) {
  const days = new Map<string, Row>();
  for (const s of series) {
    const name = labelOf(s);
    for (const p of s.points) {
      const day = p.period_start.slice(0, 10);
      const row = days.get(day) ?? { day, label: formatDayLabel(p.period_start) };
      row[name] = Number(p.value);
      row[`${name}__exact`] = p.value;
      if (!p.is_final) row[`${name}__provisional`] = 1;
      days.set(day, row);
    }
  }
  return [...days.values()].sort((a, b) => a.day.localeCompare(b.day));
}

export function TrendChart({
  title,
  series,
  unit,
  labelOf,
  kind = "line",
  height = 240,
}: {
  title: string;
  series: Series[];
  unit: string;
  labelOf: (s: Series) => string;
  kind?: "line" | "bar";
  height?: number;
}) {
  const names = series.map(labelOf);
  const rows = toRows(series, labelOf);
  const hasProvisional = rows.some((r) => names.some((n) => r[`${n}__provisional`]));
  const Chart = kind === "bar" ? BarChart : LineChart;

  return (
    <figure style={{ margin: 0 }}>
      <figcaption className="visually-hidden">{title}</figcaption>
      <div className="chart-box" style={{ height }} role="img" aria-label={`${title}. Exact values are in the data table below.`}>
        <ResponsiveContainer width="100%" height="100%">
          <Chart data={rows} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--text-3)" }} tickLine={false} axisLine={false} minTickGap={16} />
            <YAxis
              tick={{ fontSize: 11, fill: "var(--text-3)" }}
              tickLine={false}
              axisLine={false}
              width={64}
              tickFormatter={(v: number) => formatAxis(v, unit)}
            />
            <Tooltip
              contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12.5 }}
              formatter={(_value, name, item) => {
                const exact = (item?.payload as Row | undefined)?.[`${String(name)}__exact`];
                return [formatAmount(typeof exact === "string" ? exact : String(_value), unit), String(name)];
              }}
            />
            {names.map((name, i) =>
              kind === "bar" ? (
                <Bar key={name} dataKey={name} stackId="a" fill={COLORS[i % COLORS.length]} isAnimationActive={false} />
              ) : (
                <Line key={name} type="monotone" dataKey={name} stroke={COLORS[i % COLORS.length]} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
              ),
            )}
          </Chart>
        </ResponsiveContainer>
      </div>
      <div className="chart-legend">
        {names.map((name, i) => (
          <span key={name}>
            <span className="legend-swatch" style={{ background: COLORS[i % COLORS.length] }} aria-hidden="true" />
            {name}
          </span>
        ))}
        {hasProvisional && <span className="muted">Recent days are provisional and may still change.</span>}
      </div>
      <details className="chart-data">
        <summary>Show data table</summary>
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable region must be keyboard reachable */}
        <div className="table-wrap" style={{ marginTop: 8, maxHeight: 260, overflow: "auto" }} tabIndex={0} role="region" aria-label={`${title} data`}>
          <table>
            <caption className="visually-hidden">{title} data</caption>
            <thead>
              <tr>
                <th scope="col">Day (UTC)</th>
                {names.map((n) => (
                  <th key={n} scope="col" className="num">
                    {n}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.day}>
                  <td>{r.day}</td>
                  {names.map((n) => (
                    <td key={n} className="num">
                      {typeof r[`${n}__exact`] === "string" ? formatAmount(r[`${n}__exact`] as string, unit) : "—"}
                      {r[`${n}__provisional`] ? " *" : ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
