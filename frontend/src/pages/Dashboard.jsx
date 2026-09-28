import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { RefreshCw, Table2, BarChart3 } from "lucide-react";
import { errorMessage, getMetrics } from "../api/client";
import { formatDuration, formatPct } from "../lib/format";

function MetricCard({ label, value, hint }) {
  return (
    <div className="rounded-xl border border-stone-200 bg-white p-5 dark:border-stone-800 dark:bg-stone-900">
      <div className="text-sm text-stone-500 dark:text-stone-400">{label}</div>
      <div className="mt-2 text-3xl font-semibold">{value}</div>
      {hint && <div className="mt-1 text-xs text-stone-500 dark:text-stone-400">{hint}</div>}
    </div>
  );
}

// Tooltip: value leads (strong), category follows (secondary), keyed with a short stroke.
function ChartTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const { name, value } = payload[0].payload;
  return (
    <div className="rounded-lg border border-stone-200 bg-white px-3 py-2 text-sm shadow-md dark:border-stone-700 dark:bg-stone-900">
      <div className="flex items-center gap-2">
        <span className="h-0.5 w-3 rounded" style={{ background: "var(--series-1)" }} aria-hidden="true" />
        <span className="font-semibold tabular-nums" style={{ color: "var(--text-primary)" }}>{value}</span>
        <span style={{ color: "var(--text-secondary)" }}>{name}</span>
      </div>
    </div>
  );
}

function OutcomeChart({ data }) {
  const [asTable, setAsTable] = useState(false);
  const empty = data.every((d) => d.value === 0);
  return (
    <section className="viz-root rounded-xl border border-stone-200 p-5 dark:border-stone-800" style={{ background: "var(--surface-1)" }}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>Workflows by outcome</h2>
          <p className="mt-0.5 text-xs" style={{ color: "var(--text-secondary)" }}>
            Number of workflows. A run can count in both “Needed approval” and “Failed”.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setAsTable((t) => !t)}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-stone-100 dark:hover:bg-stone-800"
          style={{ color: "var(--text-secondary)" }}
          aria-pressed={asTable}
        >
          {asTable ? <BarChart3 className="size-3.5" aria-hidden="true" /> : <Table2 className="size-3.5" aria-hidden="true" />}
          {asTable ? "Chart" : "Table"}
        </button>
      </div>

      {asTable ? (
        <table className="mt-4 w-full text-sm">
          <thead>
            <tr style={{ color: "var(--text-secondary)" }}>
              <th scope="col" className="py-1.5 text-left font-medium">Outcome</th>
              <th scope="col" className="py-1.5 text-right font-medium">Workflows</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.name} className="border-t" style={{ borderColor: "var(--gridline)", color: "var(--text-primary)" }}>
                <td className="py-1.5">{d.name}</td>
                <td className="py-1.5 text-right tabular-nums">{d.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : empty ? (
        <p className="py-16 text-center text-sm" style={{ color: "var(--text-secondary)" }}>No finished workflows yet.</p>
      ) : (
        <div className="mt-4 h-64" role="img" aria-label={data.map((d) => `${d.name}: ${d.value}`).join(", ")}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 24, right: 8, bottom: 0, left: -16 }} barCategoryGap="30%">
              <CartesianGrid vertical={false} stroke="var(--gridline)" strokeWidth={1} />
              <XAxis
                dataKey="name"
                tickLine={false}
                axisLine={{ stroke: "var(--baseline)" }}
                tick={{ fill: "var(--text-secondary)", fontSize: 12 }}
              />
              <YAxis
                allowDecimals={false}
                tickLine={false}
                axisLine={false}
                tick={{ fill: "var(--text-muted)", fontSize: 12 }}
                width={40}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ fill: "var(--gridline)", opacity: 0.5 }} />
              <Bar
                dataKey="value"
                fill="var(--series-1)"
                radius={[4, 4, 0, 0]}
                maxBarSize={64}
                activeBar={{ fill: "var(--series-1-hover)" }}
                isAnimationActive={false}
              >
                <LabelList dataKey="value" position="top" style={{ fill: "var(--text-primary)", fontSize: 12, fontWeight: 600 }} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

export default function Dashboard() {
  const [m, setM] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setM(await getMetrics());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const chartData = m
    ? [
        { name: "Auto-completed", value: m.auto_completed_workflows },
        { name: "Needed approval", value: m.needed_approval_workflows },
        { name: "Failed", value: (m.by_status.failed ?? 0) + (m.by_status.interrupted ?? 0) },
      ]
    : [];

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">How much of the work runs without a human.</p>
        </div>
        <button
          type="button"
          onClick={load}
          className="rounded-lg border border-stone-300 bg-white p-2 text-stone-600 hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-300"
          aria-label="Refresh metrics"
        >
          <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
        </button>
      </div>

      {error && <p role="alert" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">{error}</p>}

      {m && (
        <>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard
              label="Auto-completed"
              value={formatPct(m.auto_completed_pct)}
              hint={`${m.auto_completed_workflows} of ${m.finished_workflows} finished, no human needed`}
            />
            <MetricCard
              label="Needed approval"
              value={formatPct(m.needed_approval_pct)}
              hint={`${m.needed_approval_workflows} of ${m.total_workflows} workflows`}
            />
            <MetricCard label="Avg steps per workflow" value={m.avg_steps_per_workflow ?? "—"} hint="Executed steps, all rounds" />
            <MetricCard
              label="Avg completion time"
              value={formatDuration(m.avg_completion_time_seconds)}
              hint="Completed runs, incl. approval wait"
            />
          </div>

          <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <OutcomeChart data={chartData} />
            <section className="rounded-xl border border-stone-200 bg-white p-5 dark:border-stone-800 dark:bg-stone-900">
              <h2 className="text-sm font-semibold">By status</h2>
              <ul className="mt-3 space-y-2 text-sm">
                {Object.entries(m.by_status).length === 0 && <li className="text-stone-500">No workflows yet.</li>}
                {Object.entries(m.by_status).map(([status, n]) => (
                  <li key={status}>
                    <Link to={`/workflows?status=${status}`} className="flex justify-between rounded px-1 hover:bg-stone-50 dark:hover:bg-stone-800">
                      <span className="capitalize text-stone-600 dark:text-stone-300">{status.replace("_", " ")}</span>
                      <span className="font-medium tabular-nums">{n}</span>
                    </Link>
                  </li>
                ))}
              </ul>
              <div className="mt-4 border-t border-stone-100 pt-3 text-sm text-stone-500 dark:border-stone-800">
                Total <span className="float-right font-medium tabular-nums text-stone-900 dark:text-stone-100">{m.total_workflows}</span>
              </div>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
