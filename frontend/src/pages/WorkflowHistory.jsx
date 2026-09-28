import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronLeft, ChevronRight, Inbox, Loader2, Plus, RefreshCw } from "lucide-react";
import { errorMessage, listWorkflows } from "../api/client";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import { WORKFLOW_STATUSES, formatDateTime, formatDuration } from "../lib/format";

const PAGE_SIZE = 20;

export default function WorkflowHistory() {
  // Filter + page live in the URL, so they survive reloads and back/forward.
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const page = Math.max(0, Number(params.get("page") || 0));
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await listWorkflows({ status: status || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [status, page]);

  useEffect(() => {
    load();
  }, [load]);

  const update = (next) => {
    const p = new URLSearchParams(params);
    Object.entries(next).forEach(([k, v]) => (v === "" || v == null || v === 0 ? p.delete(k) : p.set(k, String(v))));
    setParams(p);
  };

  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">History</h1>
          <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">All workflows run through the API, newest first.</p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="status-filter" className="text-sm text-stone-500">Status</label>
          <select
            id="status-filter"
            value={status}
            onChange={(e) => update({ status: e.target.value, page: 0 })}
            className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm dark:border-stone-700 dark:bg-stone-900"
          >
            <option value="">All</option>
            {WORKFLOW_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
          </select>
          <button
            type="button"
            onClick={load}
            className="rounded-lg border border-stone-300 bg-white p-2 text-stone-600 hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-300"
            aria-label="Refresh"
          >
            <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
        </div>
      </div>

      {error && <p role="alert" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">{error}</p>}

      <div className="mt-6 overflow-x-auto rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-stone-200 text-xs uppercase tracking-wide text-stone-500 dark:border-stone-800 dark:text-stone-400">
            <tr>
              <th scope="col" className="px-4 py-3 font-medium">Task</th>
              <th scope="col" className="px-4 py-3 font-medium">Status</th>
              <th scope="col" className="px-4 py-3 text-right font-medium">Steps</th>
              <th scope="col" className="px-4 py-3 text-right font-medium">Duration</th>
              <th scope="col" className="px-4 py-3 font-medium">Started</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-100 dark:divide-stone-800">
            {data?.items.map((w) => (
              <tr
                key={w.workflow_id}
                onClick={() => navigate(`/workflows/${w.workflow_id}`)}
                className="cursor-pointer hover:bg-stone-50 dark:hover:bg-stone-800/50"
              >
                <td className="max-w-md px-4 py-3">
                  <Link to={`/workflows/${w.workflow_id}`} className="line-clamp-2 font-medium hover:text-blue-600" onClick={(e) => e.stopPropagation()}>
                    {w.task}
                  </Link>
                  <div className="mt-0.5 font-mono text-xs text-stone-400">{w.workflow_id}</div>
                </td>
                <td className="px-4 py-3"><StatusBadge status={w.status} /></td>
                <td className="px-4 py-3 text-right tabular-nums">{w.step_count}</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatDuration(w.duration_seconds)}</td>
                <td className="whitespace-nowrap px-4 py-3 text-stone-500 dark:text-stone-400">{formatDateTime(w.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {loading && !data && (
          <div className="flex items-center justify-center gap-2 py-12 text-sm text-stone-500">
            <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Loading…
          </div>
        )}
        {data && data.items.length === 0 && (
          <div className="py-12 text-center text-sm text-stone-500">
            <Inbox className="mx-auto mb-2 size-8 text-stone-300" aria-hidden="true" />
            {status ? `No ${statusLabel(status).toLowerCase()} workflows.` : "No workflows yet."}
            {!status && (
              <div className="mt-3">
                <Link to="/submit" className="inline-flex items-center gap-1 font-medium text-blue-600 hover:underline">
                  <Plus className="size-4" aria-hidden="true" /> Run your first task
                </Link>
              </div>
            )}
          </div>
        )}
      </div>

      {total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between text-sm text-stone-500">
          <span className="tabular-nums">
            {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of {total}
          </span>
          <div className="flex gap-2">
            <button type="button" disabled={page === 0} onClick={() => update({ page: page - 1 })}
              className="inline-flex items-center gap-1 rounded-lg border border-stone-300 px-3 py-1.5 disabled:opacity-40 dark:border-stone-700">
              <ChevronLeft className="size-4" aria-hidden="true" /> Previous
            </button>
            <button type="button" disabled={page + 1 >= pages} onClick={() => update({ page: page + 1 })}
              className="inline-flex items-center gap-1 rounded-lg border border-stone-300 px-3 py-1.5 disabled:opacity-40 dark:border-stone-700">
              Next <ChevronRight className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
