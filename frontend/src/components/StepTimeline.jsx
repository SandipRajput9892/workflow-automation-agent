import { useState } from "react";
import { CheckCircle2, ChevronDown, Circle, Loader2, MinusCircle, RotateCcw, XCircle } from "lucide-react";
import { formatTime } from "../lib/format";

// pending / running / success / failed (+ skipped), each with its own icon and label.
const STEP_STATE = {
  pending: { label: "Pending", icon: Circle, iconCls: "text-stone-400 dark:text-stone-500", line: "border-stone-200 dark:border-stone-800" },
  running: { label: "Running", icon: Loader2, iconCls: "text-blue-600 dark:text-blue-400 animate-spin", line: "border-blue-200 dark:border-blue-900" },
  completed: { label: "Success", icon: CheckCircle2, iconCls: "text-green-600 dark:text-green-500", line: "border-green-200 dark:border-green-900" },
  failed: { label: "Failed", icon: XCircle, iconCls: "text-red-600 dark:text-red-400", line: "border-red-200 dark:border-red-900" },
  skipped: { label: "Skipped", icon: MinusCircle, iconCls: "text-stone-400 dark:text-stone-500", line: "border-stone-200 dark:border-stone-800" },
};

function JsonBlock({ label, value }) {
  if (value == null) return null;
  return (
    <div>
      <div className="mb-1 text-xs font-medium text-stone-500 dark:text-stone-400">{label}</div>
      <pre className="overflow-x-auto rounded-md bg-stone-100 p-2.5 text-xs leading-relaxed text-stone-800 dark:bg-stone-900 dark:text-stone-200">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function Step({ step, isLast }) {
  const [open, setOpen] = useState(false);
  const meta = STEP_STATE[step.status] ?? STEP_STATE.pending;
  const Icon = meta.icon;
  const hasDetails = step.tool_input || step.result || step.error;
  const retried = step.attempts > 1;

  return (
    <li className="relative flex gap-3">
      {!isLast && <span className={`absolute left-[11px] top-7 h-[calc(100%-1.25rem)] border-l-2 ${meta.line}`} aria-hidden="true" />}
      <Icon className={`relative mt-0.5 size-6 shrink-0 bg-white dark:bg-stone-950 ${meta.iconCls}`} aria-hidden="true" />
      <div className="min-w-0 flex-1 pb-5">
        <button
          type="button"
          onClick={() => hasDetails && setOpen((o) => !o)}
          aria-expanded={hasDetails ? open : undefined}
          className={`flex w-full items-start justify-between gap-3 text-left ${hasDetails ? "cursor-pointer" : "cursor-default"}`}
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">Step {step.step_id}</span>
              <code className="rounded bg-stone-100 px-1.5 py-0.5 text-xs text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                {step.tool_name}
              </code>
              <span className="text-xs text-stone-500 dark:text-stone-400">{meta.label}</span>
              {retried && (
                <span className="inline-flex items-center gap-1 text-xs text-amber-700 dark:text-amber-400">
                  <RotateCcw className="size-3" aria-hidden="true" /> retried
                </span>
              )}
            </div>
            <p className="mt-0.5 text-sm text-stone-600 dark:text-stone-300">{step.description}</p>
            {step.status === "failed" && step.error && (
              <p className="mt-1 text-sm text-red-700 dark:text-red-400">{step.error}</p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-xs tabular-nums text-stone-400">{formatTime(step.finished_at || step.started_at)}</span>
            {hasDetails && (
              <ChevronDown className={`size-4 text-stone-400 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
            )}
          </div>
        </button>
        {open && (
          <div className="mt-3 space-y-3">
            <JsonBlock label="Input" value={step.tool_input} />
            <JsonBlock label="Result" value={step.result} />
          </div>
        )}
      </div>
    </li>
  );
}

/** plans: [{ round, source, steps: [...] }] - one section per plan round. */
export default function StepTimeline({ plans }) {
  if (!plans.length) {
    return (
      <div className="flex items-center gap-2 py-6 text-sm text-stone-500">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Waiting for a plan…
      </div>
    );
  }
  return (
    <div className="space-y-6">
      {plans.map((plan) => (
        <section key={plan.round}>
          {plans.length > 1 && (
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-stone-500 dark:text-stone-400">
              {plan.round === 0 ? "Original plan" : `Round ${plan.round} · ${plan.source === "reflection" ? "correction" : "new plan"}`}
            </h3>
          )}
          <ol>
            {plan.steps.map((step, i) => (
              <Step key={step.step_id} step={step} isLast={i === plan.steps.length - 1} />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
