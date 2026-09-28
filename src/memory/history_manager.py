"""Execution history and long-term workflow memory.

- save_workflow_run() / get_similar_past_workflows(): store finished workflows
  in the ChromaDB vector store and retrieve similar successful ones, which the
  planner uses as examples when writing a new plan.
- HistoryManager: one append-only JSONL file per run (logs/runs/<run_id>.jsonl)
  with every agent decision, so any workflow can be replayed.
- AuditLog: a single logs/audit_log.json with every action the executor took
  (tool calls, input adjustments, skipped steps) across all runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src import json_store
from src.config import get_settings
from src.memory.vector_store import WorkflowMemory, render_plan
from src.schemas import FinalReport, PastWorkflow, Plan, Step, StepStatus


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class HistoryManager:
    def __init__(self, log_dir: Path):
        self.runs_dir = log_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.jsonl"

    def record(self, run_id: str, event: str, **data: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **_jsonable(data)}
        with self._path(run_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def load(self, run_id: str) -> list[dict[str, Any]]:
        path = self._path(run_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def list_runs(self) -> list[str]:
        return sorted((p.stem for p in self.runs_dir.glob("*.jsonl")), reverse=True)


class AuditLog:
    """Every action the executor takes, across all runs, in logs/audit_log.json.

    The file is a single JSON array of entries:
        {timestamp, run_id, plan_round, action, step_id, step, tool, input, result, attempt}
    where action is "tool_call", "input_adjusted", or "step_skipped", plan_round
    is 0 for the original plan and 1, 2, ... for corrective plans, and attempt
    numbers the tool calls within a step (1 = first call, 2 = retry, 0 = none).
    """

    def __init__(self, path: Path):
        self.path = path

    def record(
        self,
        action: str,
        *,
        step_id: int,
        step: str,
        tool: str,
        input: dict[str, Any] | None,
        result: Any,
        attempt: int,
        plan_round: int = 0,
        run_id: str | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "plan_round": plan_round,
            "action": action,
            "step_id": step_id,
            "step": step,
            "tool": tool,
            "input": _jsonable(input),
            "result": _jsonable(result),
            "attempt": attempt,
        }
        json_store.update(self.path, [], lambda entries: entries.append(entry))

    def load(self) -> list[dict[str, Any]]:
        return json_store.load(self.path, [])


# ---------------------------------------------------------------------------
# Long-term workflow memory (vector store)
# ---------------------------------------------------------------------------

_default_store: WorkflowMemory | None = None


def default_store() -> WorkflowMemory:
    """The ChromaDB store configured in settings, created on first use."""
    global _default_store
    if _default_store is None:
        s = get_settings()
        _default_store = WorkflowMemory(s.chroma_dir, s.chroma_collection, s.embedding_backend, s.memory_max_distance)
    return _default_store


def save_workflow_run(
    task: str,
    plan: Plan,
    result: FinalReport,
    *,
    run_id: str | None = None,
    status: str | None = None,
    store: WorkflowMemory | None = None,
) -> str:
    """Embed and store a finished workflow: the task, its final plan (use
    final_plan() to build one from a run's executed plans) and the result.
    Saving again with the same run_id overwrites. Returns the run_id."""
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    status = status or ("completed" if result.success else "failed")
    (store or default_store()).add(run_id, task, plan, result.summary, result.success, status)
    return run_id


def get_similar_past_workflows(
    task: str,
    top_k: int = 3,
    *,
    successful_only: bool = True,
    store: WorkflowMemory | None = None,
) -> list[PastWorkflow]:
    """The `top_k` stored workflows most similar to `task`, nearest first.
    By default only successful ones, since the planner uses them as examples."""
    return (store or default_store()).search(task, top_k, successful_only)


def final_plan(plans: list[Plan]) -> Plan:
    """What a run actually did: every completed step across the original and any
    corrective plans, in execution order, with the input of the call that
    succeeded (placeholders resolved, retries applied), renumbered from 1."""
    steps: list[Step] = []
    for plan in plans:
        for step in plan.steps:
            if step.status != StepStatus.COMPLETED or step.result is None:
                continue
            call = step.result.tool_calls[-1]
            steps.append(step.model_copy(update={"id": len(steps) + 1, "tool_input": call.tool_input}))
    goal = plans[0].goal if plans else ""
    return Plan(goal=goal, steps=steps)


def format_past_workflows(workflows: list[PastWorkflow]) -> str:
    """Render retrieved workflows for a prompt."""
    blocks = []
    for n, w in enumerate(workflows, 1):
        blocks.append(
            f"[{n}] Task: {w.task}\n"
            f"Plan that was executed:\n{render_plan(w.plan)}\n"
            f"Outcome ({w.status}): {w.outcome}"
        )
    return "\n\n".join(blocks)
