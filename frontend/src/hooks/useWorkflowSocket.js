import { useEffect, useReducer, useRef, useState } from "react";
import { wsUrl } from "../api/client";

/*
 * Live view of one workflow over WS /ws/workflows/{id}.
 *
 * The server sends a `snapshot` (workflow + step logs from the DB), then replays
 * every event emitted so far, then streams new ones. On reconnect it does the
 * same, so the reducer rebuilds from scratch on each snapshot: reconnecting can
 * never duplicate steps or log lines.
 */

const FINAL_KINDS = new Set(["run_completed", "run_failed", "run_rejected"]);
const EVENT_STEP_STATUS = { ok: "completed", failed: "failed", skipped: "skipped" };

const initialState = { workflow: null, status: null, plans: {}, events: [] };

function upsertStep(plans, round, stepId, patch, planMeta = {}) {
  const plan = plans[round] || { round, source: round === 0 ? "planner" : "reflection", steps: {} };
  const prev = plan.steps[stepId] || { step_id: stepId, status: "pending" };
  return {
    ...plans,
    [round]: { ...plan, ...planMeta, steps: { ...plan.steps, [stepId]: { ...prev, ...patch } } },
  };
}

/** Merge the DB's step logs (and any plan awaiting approval) into the plan map. */
function mergeWorkflow(plans, workflow) {
  let next = plans;
  for (const s of workflow.steps || []) {
    next = upsertStep(next, s.plan_round, s.step_id, { ...s });
  }
  const pending = workflow.pending_approval?.plan;
  if (pending) {
    for (const s of pending.steps) {
      if (!next[pending.round]?.steps?.[s.id]) {
        next = upsertStep(next, pending.round, s.id, {
          tool_name: s.tool_name, description: s.description, tool_input: s.tool_input, status: "pending",
        });
      }
    }
  }
  return next;
}

function reducer(state, msg) {
  if (msg.kind === "snapshot") {
    const wf = msg.workflow;
    return { workflow: wf, status: wf?.status ?? "running", plans: wf ? mergeWorkflow({}, wf) : {}, events: [] };
  }

  let { plans, status, workflow } = state;
  const d = msg.data || {};
  switch (msg.kind) {
    case "run_started":
    case "run_resumed":
    case "approval":
      status = "running";
      break;
    case "plan_created":
      for (const s of d.steps || []) {
        const known = plans[d.round]?.steps?.[s.step_id];
        // Never downgrade a step we've already seen running/finished back to pending.
        plans = upsertStep(plans, d.round, s.step_id, known ? {} : { ...s, status: "pending" }, { source: d.source });
      }
      break;
    case "step_started":
      plans = upsertStep(plans, d.round, d.step_id, {
        tool_name: d.tool_name, description: d.description, status: "running", started_at: msg.ts,
      });
      break;
    case "step_finished":
      plans = upsertStep(plans, d.round, d.step_id, {
        tool_name: d.tool_name,
        description: d.description,
        status: EVENT_STEP_STATUS[d.status] || "failed",
        tool_input: d.tool_input,
        result: d.result,
        error: d.error,
        attempts: d.attempts,
        summary: msg.message,
        finished_at: msg.ts,
      });
      break;
    case "awaiting_approval":
      status = "awaiting_approval";
      break;
    default:
      break;
  }
  if (msg.workflow) {
    // Final event of a run()/resume() call carries the authoritative workflow.
    workflow = msg.workflow;
    status = workflow.status;
    plans = mergeWorkflow(plans, workflow);
  }
  return { workflow, status, plans, events: [...state.events, msg] };
}

export function useWorkflowSocket(workflowId) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [connection, setConnection] = useState("connecting"); // connecting | open | closed | not_found
  const finishedRef = useRef(false);

  useEffect(() => {
    let ws;
    let retry = 0;
    let timer;
    let disposed = false;
    finishedRef.current = false;
    dispatch({ kind: "snapshot", workflow: null });

    const connect = () => {
      setConnection("connecting");
      ws = new WebSocket(wsUrl(`/ws/workflows/${encodeURIComponent(workflowId)}`));
      ws.onopen = () => {
        retry = 0;
        setConnection("open");
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (FINAL_KINDS.has(msg.kind) || (msg.kind === "snapshot" && msg.workflow &&
            ["completed", "failed", "rejected"].includes(msg.workflow.status))) {
          finishedRef.current = true;
        }
        dispatch(msg);
      };
      ws.onclose = (e) => {
        if (disposed) return;
        if (e.code === 4404) return setConnection("not_found");
        if (finishedRef.current) return setConnection("closed");
        // Unexpected drop (server restart, network): reconnect with backoff.
        setConnection("connecting");
        timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      };
    };
    connect();

    return () => {
      disposed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [workflowId]);

  const plans = Object.values(state.plans)
    .sort((a, b) => a.round - b.round)
    .map((p) => ({ ...p, steps: Object.values(p.steps).sort((a, b) => a.step_id - b.step_id) }));

  return { ...state, plans, connection };
}
