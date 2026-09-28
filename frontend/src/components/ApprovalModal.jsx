import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, ShieldAlert, X } from "lucide-react";
import { approveWorkflow, errorMessage } from "../api/client";

const RISK = {
  high: "bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300",
  medium: "bg-amber-50 text-amber-800 ring-amber-600/25 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-green-50 text-green-800 ring-green-600/20 dark:bg-green-950 dark:text-green-300",
};

/**
 * Shown while a workflow is paused for human approval. `request` is the
 * workflow's pending_approval: { plan, gate } from the backend.
 * Reject with a comment -> the planner revises the plan; without -> cancelled.
 */
export default function ApprovalModal({ workflowId, request, onClose, onDecided }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(null); // "approve" | "reject" | null
  const [error, setError] = useState("");
  const approveRef = useRef(null);

  useEffect(() => {
    approveRef.current?.focus();
    const onKey = (e) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const decide = async (approved) => {
    setBusy(approved ? "approve" : "reject");
    setError("");
    try {
      await approveWorkflow(workflowId, { approved, comment: approved ? "" : comment.trim() });
      onDecided?.(approved);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(null);
    }
  };

  const { plan, gate } = request;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/50 p-4 sm:items-center" onClick={() => !busy && onClose()}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl bg-white shadow-xl ring-1 ring-stone-900/10 dark:bg-stone-900 dark:ring-white/10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-stone-200 px-5 py-4 dark:border-stone-800">
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 size-5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden="true" />
            <div>
              <h2 id="approval-title" className="font-semibold">Approval needed</h2>
              <p className="mt-0.5 text-sm text-stone-500 dark:text-stone-400">
                Nothing in this plan has run yet. It will only execute if you approve it.
              </p>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={!!busy} className="rounded p-1 text-stone-400 hover:bg-stone-100 hover:text-stone-600 dark:hover:bg-stone-800" aria-label="Close">
            <X className="size-5" />
          </button>
        </div>

        <div className="space-y-4 overflow-y-auto px-5 py-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-stone-500 dark:text-stone-400">Risk</span>
            <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium capitalize ring-1 ring-inset ${RISK[gate.risk_level] ?? RISK.medium}`}>
              <AlertTriangle className="size-3" aria-hidden="true" /> {gate.risk_level}
            </span>
          </div>

          {gate.concerns?.length > 0 && (
            <ul className="space-y-1.5 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/60 dark:text-amber-200">
              {gate.concerns.map((c, i) => (
                <li key={i} className="flex gap-2">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" /> <span>{c}</span>
                </li>
              ))}
            </ul>
          )}

          <div>
            <h3 className="mb-2 text-sm font-medium">Planned steps</h3>
            <ol className="space-y-2">
              {plan.steps.map((s) => (
                <li key={s.id} className="rounded-lg border border-stone-200 p-3 dark:border-stone-800">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="font-medium">{s.id}.</span>
                    <code className="rounded bg-stone-100 px-1.5 py-0.5 text-xs dark:bg-stone-800">{s.tool_name}</code>
                  </div>
                  <p className="mt-1 text-sm text-stone-600 dark:text-stone-300">{s.description}</p>
                  <pre className="mt-2 overflow-x-auto rounded bg-stone-50 p-2 text-xs text-stone-700 dark:bg-stone-950 dark:text-stone-300">
                    {JSON.stringify(s.tool_input, null, 2)}
                  </pre>
                </li>
              ))}
            </ol>
          </div>

          <div>
            <label htmlFor="approval-comment" className="text-sm font-medium">
              Comment <span className="font-normal text-stone-500">(optional)</span>
            </label>
            <textarea
              id="approval-comment"
              rows={2}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="e.g. Post in #leads instead of #sales"
              className="mt-1 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-stone-700 dark:bg-stone-950"
            />
            <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
              Rejecting with a comment asks the planner for a revised plan. Rejecting without one cancels the workflow.
            </p>
          </div>

          {error && <p role="alert" className="text-sm text-red-700 dark:text-red-400">{error}</p>}
        </div>

        <div className="flex justify-end gap-2 border-t border-stone-200 bg-stone-50 px-5 py-3 dark:border-stone-800 dark:bg-stone-900/60">
          <button
            type="button"
            onClick={() => decide(false)}
            disabled={!!busy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-stone-300 bg-white px-4 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-stone-700 dark:bg-stone-900 dark:text-red-400 dark:hover:bg-red-950"
          >
            <X className="size-4" aria-hidden="true" />
            {busy === "reject" ? "Rejecting…" : comment.trim() ? "Reject & revise" : "Reject"}
          </button>
          <button
            ref={approveRef}
            type="button"
            onClick={() => decide(true)}
            disabled={!!busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            <Check className="size-4" aria-hidden="true" />
            {busy === "approve" ? "Approving…" : "Approve & run"}
          </button>
        </div>
      </div>
    </div>
  );
}
