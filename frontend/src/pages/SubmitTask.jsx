import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Loader2, SendHorizontal } from "lucide-react";
import { createWorkflow, errorMessage } from "../api/client";

const EXAMPLES = [
  "We just met Priya Shah (priya.shah@acmecorp.com), VP Operations at Acme Corp. Add her as a lead, mark her as contacted, send her a friendly follow-up email, and let the team know in #sales.",
  "Book a 'Product demo - Globex' on October 6 2026 at 2pm with tom.lee@globex.io and jordan@ourco.com, email Tom to confirm, and post the booking in #sales.",
  "Schedule a 'Q4 pipeline kickoff' for 2026-10-01 at 10:00 with alex@ourco.com, sam@ourco.com and jordan@ourco.com, and post the details in #general.",
];
const MAX = 5000;
const REDIRECT_MS = 1500;

export default function SubmitTask() {
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!created) return;
    const t = setTimeout(() => navigate(`/workflows/${created}`), REDIRECT_MS);
    return () => clearTimeout(t);
  }, [created, navigate]);

  const submit = async (e) => {
    e?.preventDefault();
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const { workflow_id } = await createWorkflow(task.trim());
      setCreated(workflow_id);
    } catch (err) {
      setError(errorMessage(err));
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold">New task</h1>
      <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
        Describe what should happen in plain language. The agent plans the steps across email, calendar, CRM and
        Slack, asks for approval when a plan is high-risk, and checks the result.
      </p>

      <form onSubmit={submit} className="mt-6">
        <label htmlFor="task" className="sr-only">Task</label>
        <textarea
          id="task"
          rows={7}
          maxLength={MAX}
          value={task}
          disabled={submitting}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && submit(e)}
          placeholder="e.g. Add Rohan Mehta (rohan@nimbus.io) from Nimbus Labs to the CRM, send him a welcome email, and schedule a kickoff call next Monday at 11am."
          className="w-full resize-y rounded-xl border border-stone-300 bg-white px-4 py-3 text-sm shadow-sm outline-none placeholder:text-stone-400 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 disabled:opacity-60 dark:border-stone-700 dark:bg-stone-900"
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <span className="text-xs text-stone-500 tabular-nums">
            {task.length}/{MAX} · Ctrl+Enter to submit
          </span>
          <button
            type="submit"
            disabled={!task.trim() || submitting}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : <SendHorizontal className="size-4" aria-hidden="true" />}
            {submitting ? "Starting…" : "Run workflow"}
          </button>
        </div>
      </form>

      {error && (
        <p role="alert" className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {created && (
        <div role="status" className="mt-4 flex items-start gap-3 rounded-lg bg-green-50 px-4 py-3 text-sm text-green-800 dark:bg-green-950 dark:text-green-300">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <div>
            Workflow started: <code className="font-mono font-medium">{created}</code>
            <div className="text-green-700/80 dark:text-green-400/80">Opening the live view…</div>
          </div>
        </div>
      )}

      {!created && (
        <div className="mt-8">
          <h2 className="text-sm font-medium text-stone-500 dark:text-stone-400">Try an example</h2>
          <div className="mt-2 grid gap-2">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                disabled={submitting}
                onClick={() => setTask(ex)}
                className="rounded-lg border border-stone-200 bg-white px-4 py-3 text-left text-sm text-stone-600 hover:border-blue-300 hover:bg-blue-50/50 disabled:opacity-50 dark:border-stone-800 dark:bg-stone-900 dark:text-stone-300 dark:hover:border-blue-800 dark:hover:bg-blue-950/40"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
