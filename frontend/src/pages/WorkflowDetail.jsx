import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, CircleAlert, ShieldAlert, Wifi, WifiOff } from "lucide-react";
import { useWorkflowSocket } from "../hooks/useWorkflowSocket";
import { getWorkflow } from "../api/client";
import StepTimeline from "../components/StepTimeline";
import StatusBadge from "../components/StatusBadge";
import ApprovalModal from "../components/ApprovalModal";
import { formatDateTime, formatDuration, formatTime } from "../lib/format";
import { rememberWorkflow } from "../lib/recent";

function ConnectionPill({ connection }) {
  const live = connection === "open";
  const label = { open: "Live", connecting: "Connecting…", closed: "Final" }[connection] ?? connection;
  const Icon = live || connection === "closed" ? Wifi : WifiOff;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-stone-500 dark:text-stone-400">
      <Icon className={`size-3.5 ${live ? "text-green-600" : ""}`} aria-hidden="true" /> {label}
    </span>
  );
}

function Card({ title, children, className = "" }) {
  return (
    <section className={`rounded-xl border border-stone-200 bg-white p-5 dark:border-stone-800 dark:bg-stone-900 ${className}`}>
      {title && <h2 className="mb-4 text-sm font-semibold">{title}</h2>}
      {children}
    </section>
  );
}

export default function WorkflowDetail() {
  const { id } = useParams();
  const { workflow, status, plans, events, connection } = useWorkflowSocket(id);
  const [pendingApproval, setPendingApproval] = useState(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => rememberWorkflow(id), [id]);

  // The approval request (plan + safety review) comes with the workflow; when a
  // pause is announced before the updated workflow arrives, fetch it.
  useEffect(() => {
    if (status !== "awaiting_approval") {
      setPendingApproval(null);
      setDismissed(false);
      return;
    }
    if (workflow?.pending_approval) {
      setPendingApproval(workflow.pending_approval);
      return;
    }
    let alive = true;
    getWorkflow(id).then((wf) => alive && wf.pending_approval && setPendingApproval(wf.pending_approval)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [status, workflow, id]);

  if (connection === "not_found") {
    return (
      <div className="mx-auto max-w-xl py-16 text-center">
        <CircleAlert className="mx-auto size-10 text-stone-400" aria-hidden="true" />
        <h1 className="mt-3 text-lg font-semibold">Workflow not found</h1>
        <p className="mt-1 text-sm text-stone-500"><code>{id}</code> doesn't exist on this backend.</p>
        <Link to="/workflows" className="mt-4 inline-block text-sm font-medium text-blue-600 hover:underline">Back to history</Link>
      </div>
    );
  }

  const report = workflow?.final_report;
  const allSteps = plans.flatMap((p) => p.steps);
  const done = allSteps.filter((s) => ["completed", "failed", "skipped"].includes(s.status)).length;
  const log = events.filter((e) => e.kind !== "snapshot");

  return (
    <div className="mx-auto max-w-6xl">
      <Link to="/workflows" className="inline-flex items-center gap-1 text-sm text-stone-500 hover:text-stone-800 dark:hover:text-stone-200">
        <ArrowLeft className="size-4" aria-hidden="true" /> History
      </Link>

      <header className="mt-3 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            {status && <StatusBadge status={status} />}
            <ConnectionPill connection={connection} />
          </div>
          <h1 className="mt-2 text-lg font-semibold leading-snug">{workflow?.task ?? "Loading…"}</h1>
          <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
            <code>{id}</code>
            {workflow && <> · started {formatDateTime(workflow.created_at)}</>}
            {workflow?.duration_seconds != null && <> · took {formatDuration(workflow.duration_seconds)}</>}
          </p>
        </div>
        {pendingApproval && dismissed && (
          <button
            type="button"
            onClick={() => setDismissed(false)}
            className="inline-flex items-center gap-2 rounded-lg bg-amber-500 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-amber-600"
          >
            <ShieldAlert className="size-4" aria-hidden="true" /> Review plan
          </button>
        )}
      </header>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="space-y-6">
          <Card title={`Steps${allSteps.length ? ` · ${done}/${allSteps.length}` : ""}`}>
            <StepTimeline plans={plans} />
          </Card>

          {report && (
            <Card title="Result">
              <p className="text-sm">{report.summary}</p>
              {report.actions_taken?.length > 0 && (
                <>
                  <h3 className="mt-4 text-xs font-semibold uppercase tracking-wide text-stone-500">Actions taken</h3>
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-stone-700 dark:text-stone-300">
                    {report.actions_taken.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </>
              )}
              {report.open_issues?.length > 0 && (
                <>
                  <h3 className="mt-4 text-xs font-semibold uppercase tracking-wide text-stone-500">Open issues</h3>
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-800 dark:text-amber-300">
                    {report.open_issues.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </>
              )}
            </Card>
          )}
          {workflow?.error && (
            <Card title="Error">
              <p className="text-sm text-red-700 dark:text-red-400">{workflow.error}</p>
              <p className="mt-2 text-xs text-stone-500">
                Resume from the command line: <code>python -m src.orchestrator --resume {id}</code>
              </p>
            </Card>
          )}
        </div>

        <Card title="Activity" className="h-fit lg:sticky lg:top-6">
          {log.length === 0 ? (
            <p className="text-sm text-stone-500">No live events{status && ["completed", "failed", "rejected"].includes(status) ? " (run finished before this session)" : " yet"}.</p>
          ) : (
            <ol className="max-h-[32rem] space-y-2 overflow-y-auto text-xs" aria-live="polite">
              {log.map((e, i) => (
                <li key={i} className="flex gap-2">
                  <span className="shrink-0 tabular-nums text-stone-400">{formatTime(e.ts)}</span>
                  <span className="text-stone-700 dark:text-stone-300">{e.message}</span>
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>

      {pendingApproval && !dismissed && (
        <ApprovalModal
          workflowId={id}
          request={pendingApproval}
          onClose={() => setDismissed(true)}
          onDecided={() => setPendingApproval(null)}
        />
      )}
    </div>
  );
}
