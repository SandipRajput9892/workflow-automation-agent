"""LangGraph orchestration: a supervisor hub routing between planner, executor and reflection.

    START ─▶ supervisor ─┬─▶ planner ────────────────────────────▶ supervisor
                         ├─▶ gate ─┬─▶ executor ⟲ (one step per visit) ─▶ supervisor
                         │         ├─▶ approval ─▶ executor / planner / finalize
                         │         └─▶ planner / finalize
                         ├─▶ reflection ─────────────────────────▶ supervisor
                         └─▶ finalize ─▶ END

- supervisor: intake, then picks the next agent (SupervisorAgent.allowed_moves
  + route). Choosing the executor sends the pending plan through the gate.
- gate: policy checks + Claude's review of the pending plan.
- approval: human sign-off when approval_mode requires it. With an approver
  (e.g. the console prompt) it asks inline; without one the run PAUSES here and
  is continued later with resume(run_id, ApprovalDecision(...)).
- executor: runs ONE step per visit, so every completed step is checkpointed.

Checkpointing: the graph state is saved to SQLite after every node, keyed by
run_id. A run that crashes (or is killed) can be resumed from its last
checkpoint; completed steps are not executed again.

Progress streaming: nodes emit ProgressEvents as they work. Use stream() /
stream_resume() to iterate over them, or pass on_event= to run() / resume().

CLI:
    python -m src.orchestrator "Add Priya (priya@acme.com, Acme) as a lead and tell #sales"
    python -m src.orchestrator --workflow new_lead_onboarding [--yes | --no-wait] [--quiet]
    python -m src.orchestrator --list | --pending
    python -m src.orchestrator --approve RUN_ID | --reject RUN_ID [--comment "..."] | --resume RUN_ID
"""

from __future__ import annotations

import argparse
import enum
import inspect
import json
import logging
import sqlite3
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

import src.schemas as schemas
from src.agents.base import LLM, ClaudeLLM, LLMError
from src.agents.executor_agent import ExecutorAgent, apply_result
from src.agents.planner_agent import PlannerAgent
from src.agents.reflection_agent import ReflectionAgent, format_execution_history
from src.agents.supervisor_agent import Approver, AutoApprover, ConsoleApprover, SupervisorAgent
from src.config import Settings, get_settings, setup_logging
from src.memory.history_manager import AuditLog, HistoryManager, final_plan, save_workflow_run
from src.memory.vector_store import WorkflowMemory
from src.schemas import (
    ApprovalDecision,
    ApprovalRequest,
    FinalReport,
    GateResult,
    ProgressEvent,
    RouteRecord,
    StepStatus,
    WorkflowRun,
    WorkflowState,
    utcnow,
)
from src.tools.tool_registry import ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)

TERMINAL = ("completed", "failed", "rejected")


def checkpoint_serializer() -> JsonPlusSerializer:
    """Serializer that may rebuild our schema types from checkpoints (LangGraph
    only deserializes explicitly allowed classes)."""
    allowed = [
        (schemas.__name__, name)
        for name, obj in vars(schemas).items()
        if inspect.isclass(obj) and obj.__module__ == schemas.__name__ and issubclass(obj, (BaseModel, enum.Enum))
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def sqlite_checkpointer(path: Path) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False), serde=checkpoint_serializer())


class WorkflowOrchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        llm: LLM | None = None,
        registry: ToolRegistry | None = None,
        memory: WorkflowMemory | None = None,
        history: HistoryManager | None = None,
        audit: AuditLog | None = None,
        approver: Approver | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        today: str | None = None,
    ):
        """`approver`: asked inline when a plan needs human sign-off. If None,
        the run pauses instead (status "awaiting_approval") until resume()."""
        self.settings = settings or get_settings()
        s = self.settings
        self.llm = llm or ClaudeLLM(s)
        self.registry = registry = registry or build_default_registry(s.mock_api_dir)
        self.memory = memory or WorkflowMemory(s.chroma_dir, s.chroma_collection, s.embedding_backend, s.memory_max_distance)
        self.history = history or HistoryManager(s.log_dir)
        self.audit = audit or AuditLog(s.audit_log_path)
        self.approver = approver
        self._today = today  # fixed date for tests; otherwise the real date at each use

        self.supervisor = SupervisorAgent(self.llm, registry, s)
        self.planner = PlannerAgent(
            self.llm, registry, s.max_steps, s.max_output_attempts, memory=self.memory, memory_top_k=s.memory_top_k
        )
        self.executor = ExecutorAgent(self.llm, registry, self.audit, s.executor_max_retries)
        self.reflector = ReflectionAgent(self.llm, registry, s.max_steps, s.max_output_attempts)
        self.graph = self._build_graph(checkpointer or sqlite_checkpointer(s.checkpoint_db))

    # ============================================================== public API

    def run(self, task: str, *, run_id: str | None = None, on_event: Callable[[ProgressEvent], None] | None = None) -> WorkflowRun:
        """Run a task to completion (or until it pauses for approval)."""
        return self._consume(self.stream(task, run_id=run_id), on_event)

    def resume(
        self,
        run_id: str,
        approval: ApprovalDecision | None = None,
        *,
        on_event: Callable[[ProgressEvent], None] | None = None,
    ) -> WorkflowRun:
        """Continue a paused or interrupted run. A run awaiting approval needs `approval`."""
        return self._consume(self.stream_resume(run_id, approval), on_event)

    def stream(self, task: str, *, run_id: str | None = None) -> Iterator[ProgressEvent]:
        """Run a task, yielding progress events. The last event carries the WorkflowRun."""
        run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        initial: WorkflowState = {
            "run_id": run_id,
            "task": task,
            "started_at": utcnow(),
            "status": "running",
            "plans": [],
            "gates": [],
            "routes": [],
            "plan_requests": 0,
        }
        self.history.record(run_id, "start", task=task)
        logger.info("run %s: %s", run_id, task)
        yield ProgressEvent(kind="run_started", message=task, run_id=run_id)
        yield from self._drive(run_id, initial)

    def stream_resume(self, run_id: str, approval: ApprovalDecision | None = None) -> Iterator[ProgressEvent]:
        snapshot = self.graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise KeyError(f"Unknown run '{run_id}'")
        if snapshot.interrupts:
            if approval is None:
                raise ValueError(f"Run {run_id} is waiting for approval; pass an ApprovalDecision.")
            graph_input: Any = Command(resume=approval.model_dump(mode="json"))
        elif snapshot.next:
            graph_input = None  # interrupted by an error: continue from the last checkpoint
        else:
            run = self.get_run(run_id)
            yield ProgressEvent(kind="run_finished", message=f"Run already {run.status}", run_id=run_id, run=run)
            return
        self.history.record(run_id, "resumed", approval=approval)
        yield ProgressEvent(kind="run_resumed", message="Resuming" + (" with approval decision" if approval else ""), run_id=run_id)
        yield from self._drive(run_id, graph_input)

    def get_run(self, run_id: str, error: str | None = None) -> WorkflowRun | None:
        """The run's current state from its latest checkpoint."""
        snapshot = self.graph.get_state(self._config(run_id))
        v = snapshot.values
        if not v:
            return None
        if snapshot.interrupts:
            status = "awaiting_approval"
        elif error or snapshot.next:
            status = "interrupted"
        else:
            status = v.get("status", "failed")
        return WorkflowRun(
            run_id=v["run_id"],
            task=v["task"],
            status=status,
            plans=v.get("plans", []),
            review=v.get("review"),
            gates=v.get("gates", []),
            routes=v.get("routes", []),
            pending_approval=v.get("pending_approval") if status == "awaiting_approval" else None,
            error=error,
            final_report=v.get("final_report"),
            started_at=v["started_at"],
            finished_at=utcnow() if status in TERMINAL else None,
        )

    def pending_approvals(self) -> list[WorkflowRun]:
        runs = (self.get_run(rid) for rid in self.history.list_runs())
        return [r for r in runs if r is not None and r.status == "awaiting_approval"]

    # ============================================================= internals

    @property
    def today(self) -> str:
        # Evaluated per use, so a long-running server doesn't plan with a stale date.
        return self._today or date.today().isoformat()

    def _config(self, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": self._recursion_limit()}

    def _recursion_limit(self) -> int:
        s = self.settings
        # supervisor visit + agent visit(s) per move; the executor visits once per step.
        per_execution = 3 + s.max_steps  # gate + approval + steps + reflection
        agent_visits = s.max_plan_requests + (1 + s.max_correction_rounds) * per_execution
        return 2 * agent_visits + 20

    def _drive(self, run_id: str, graph_input: Any) -> Iterator[ProgressEvent]:
        error = None
        try:
            for event in self.graph.stream(graph_input, self._config(run_id), stream_mode="custom"):
                yield event
        except Exception as exc:
            logger.exception("run %s interrupted by an error", run_id)
            self.history.record(run_id, "error", error=repr(exc))
            error = repr(exc)

        run = self.get_run(run_id, error)
        if run is None:  # failed before the first checkpoint
            report = FinalReport(success=False, summary=f"Run crashed: {error}", actions_taken=[], open_issues=[])
            run = WorkflowRun(run_id=run_id, task="", status="interrupted", error=error, final_report=report)
        self.history.record(run_id, "workflow_run", run=run)

        if run.status == "awaiting_approval":
            msg = "Paused: waiting for a human to approve the plan"
        elif run.status == "interrupted":
            msg = f"Interrupted by an error ({error}); the run can be resumed from its last checkpoint"
        else:
            msg = f"Run {run.status}"
        yield ProgressEvent(kind=f"run_{'paused' if run.status == 'awaiting_approval' else run.status}", message=msg, run_id=run_id, run=run)

    @staticmethod
    def _consume(events: Iterator[ProgressEvent], on_event: Callable[[ProgressEvent], None] | None) -> WorkflowRun:
        run = None
        for event in events:
            if on_event:
                on_event(event)
            if event.run is not None:
                run = event.run
        assert run is not None
        return run

    def _emit(self, state: WorkflowState, kind: str, message: str, **data: Any) -> None:
        get_stream_writer()(ProgressEvent(kind=kind, message=message, run_id=state["run_id"], data=data))

    # ================================================================ nodes

    def _supervisor(self, state: WorkflowState) -> WorkflowState:
        run_id = state["run_id"]
        if "decision" not in state:
            return self._intake(state)

        task = state["decision"].normalized_task
        options, situation = self.supervisor.allowed_moves(state)
        route = self.supervisor.route(task, state, options, situation)
        routes = [*state.get("routes", []), route]
        self.history.record(run_id, "route", route=route)
        if route.by_llm:
            self._emit(state, "route", f"Supervisor chose {route.next}: {route.reasoning}", options=options)
        logger.info("supervisor -> %s (%s)", route.next, "llm" if route.by_llm else "rule")

        if route.next == "executor":
            return {"routes": routes, "next": "gate"}
        if route.next == "finish":
            return {"routes": routes, "next": "finalize", "status": self._final_status(state)}
        return {"routes": routes, "next": route.next, "guidance": route.guidance}

    def _intake(self, state: WorkflowState) -> WorkflowState:
        decision = self.supervisor.review(state["task"], self.today)
        self.history.record(state["run_id"], "supervisor_review", decision=decision)
        self._emit(
            state, "intake",
            f"Task {'approved' if decision.approved else 'rejected'} (risk {decision.risk_level}): {decision.normalized_task}",
            approved=decision.approved, risk=decision.risk_level,
        )
        nxt = "planner" if decision.approved else "finish"
        route = RouteRecord(after="start", options=[nxt], next=nxt, reasoning=decision.reasoning, by_llm=False)
        self.history.record(state["run_id"], "route", route=route)
        update: WorkflowState = {"decision": decision, "routes": [route], "next": "planner" if decision.approved else "finalize"}
        if not decision.approved:
            update["status"] = "rejected"
        return update

    def _planner(self, state: WorkflowState) -> WorkflowState:
        task = state["decision"].normalized_task
        plans = state.get("plans", [])
        context_parts = []
        if plans:
            context_parts.append(format_execution_history(plans))
        if state.get("guidance"):
            context_parts.append(f"Supervisor guidance: {state['guidance']}")
        requests = state.get("plan_requests", 0) + 1
        try:
            plan = self.planner.plan(task, self.today, context="\n\n".join(context_parts))
        except LLMError as exc:  # includes PlanningError
            logger.error("planning failed: %s", exc)
            self.history.record(state["run_id"], "planning_failed", error=str(exc))
            self._emit(state, "planning_failed", str(exc))
            return {"event": "planning_failed", "error": str(exc), "plan_requests": requests, "guidance": ""}
        plan = plan.model_copy(update={"round": len(plans)})
        self.history.record(state["run_id"], "plan", plan=plan)
        self._emit_plan(state, plan, "planner")
        return {"event": "planned", "pending_plan": plan, "plan_requests": requests, "guidance": ""}

    def _emit_plan(self, state: WorkflowState, plan, source: str) -> None:
        self._emit(
            state, "plan_created", f"{'Corrective plan' if source == 'reflection' else 'Plan'} (round {plan.round}) "
            f"with {len(plan.steps)} step(s)",
            round=plan.round, source=source,
            steps=[{"step_id": s.id, "tool_name": s.tool_name, "description": s.description, "tool_input": s.tool_input}
                   for s in plan.steps],
        )

    def _gate(self, state: WorkflowState) -> WorkflowState:
        """Safety review of the pending plan before it may execute."""
        plan = state["pending_plan"]
        gate = self.supervisor.review_plan(state["task"], plan, state.get("plans", []), state["decision"].risk_level, self.today)
        gates = [*state.get("gates", []), gate]
        self.history.record(state["run_id"], "gate", gate=gate)
        self._emit(state, "gate", f"Safety gate: {gate.decision} (risk {gate.risk_level})",
                   round=plan.round, decision=gate.decision, risk=gate.risk_level, concerns=gate.concerns)

        if self.supervisor.needs_human(gate):
            self._emit(state, "awaiting_approval", f"Plan round {plan.round} needs human approval (risk {gate.risk_level})")
            return {
                "gates": gates,
                "next": "approval",
                "status": "awaiting_approval",
                "pending_approval": ApprovalRequest(plan=plan, gate=gate),
            }
        if gate.decision == "approved":
            return {"gates": gates, "next": "executor"}
        return self._not_approved(state, gate, gates)

    def _approval(self, state: WorkflowState) -> WorkflowState:
        """Human sign-off. Without an approver this pauses the run (interrupt);
        on resume, only this node re-runs, not the gate's LLM review."""
        request = state["pending_approval"]
        if self.approver is not None:
            decision = self.approver(state["task"], request.plan, request.gate)
        else:
            raw = interrupt({"run_id": state["run_id"], "plan": request.plan.model_dump(mode="json"),
                             "gate": request.gate.model_dump(mode="json")})
            decision = ApprovalDecision.model_validate(raw)

        gate = self.supervisor.apply_approval(request.gate, decision)
        gates = [*state.get("gates", [])[:-1], gate]
        self.history.record(state["run_id"], "approval", decision=decision)
        self._emit(
            state, "approval",
            f"{decision.approver} {'approved' if decision.approved else 'declined'} the plan"
            + (f": {decision.comment}" if decision.comment else ""),
        )
        update: WorkflowState = {"gates": gates, "status": "running", "pending_approval": None}
        if gate.decision == "approved":
            return {**update, "next": "executor"}
        return {**update, **self._not_approved(state, gate, gates)}

    def _not_approved(self, state: WorkflowState, gate: GateResult, gates: list[GateResult]) -> WorkflowState:
        """Gate or human said no: back to the planner with feedback if budget allows, else stop."""
        routes = state.get("routes", [])
        if gate.decision == "revise" and state.get("plan_requests", 0) < self.settings.max_plan_requests:
            route = RouteRecord(after="gate", options=["planner"], next="planner",
                                reasoning=f"Plan sent back: {gate.feedback}", guidance=gate.feedback, by_llm=False)
            self.history.record(state["run_id"], "route", route=route)
            return {"routes": [*routes, route], "gates": gates, "next": "planner", "guidance": gate.feedback, "pending_plan": None}

        reason = "Plan rejected." if gate.decision == "rejected" else "Revision needed but planning budget exhausted."
        route = RouteRecord(after="gate", options=["finish"], next="finish", reasoning=reason, by_llm=False)
        self.history.record(state["run_id"], "route", route=route)
        status = "rejected" if gate.decision == "rejected" else "failed"
        return {"routes": [*routes, route], "gates": gates, "next": "finalize", "status": status, "pending_plan": None}

    def _executor(self, state: WorkflowState) -> WorkflowState:
        """Execute the next pending step of the pending plan (one step per visit)."""
        plan = state["pending_plan"]
        idx = next((i for i, s in enumerate(plan.steps) if s.status == StepStatus.PENDING), None)
        if idx is not None:
            step = plan.steps[idx]
            self._emit(state, "step_started", f"Step {step.id}/{len(plan.steps)}: {step.tool_name} - {step.description}",
                       round=plan.round, step_id=step.id, tool_name=step.tool_name, description=step.description)
            result = self.executor.execute_step(plan, idx, state["decision"].normalized_task, run_id=state["run_id"], today=self.today)
            plan = apply_result(plan, idx, result)
            status = "skipped" if result.skipped else "ok" if result.success else "failed"
            last = result.tool_calls[-1] if result.tool_calls else None
            # Full detail so listeners (e.g. the API's step log) needn't wait for the checkpoint.
            self._emit(state, "step_finished", f"Step {step.id} {status}: {result.summary}",
                       round=plan.round, step_id=step.id, status=status, tool_name=step.tool_name,
                       description=step.description,
                       tool_input=last.tool_input if last else step.tool_input,
                       result=last.result.model_dump(mode="json") if last else None,
                       error=result.error, attempts=len(result.tool_calls))

        if any(s.status == StepStatus.PENDING for s in plan.steps):
            return {"pending_plan": plan, "next": "executor"}
        self.history.record(state["run_id"], "executed", plan=plan)
        return {"event": "executed", "plans": [*state.get("plans", []), plan], "pending_plan": None, "next": "supervisor"}

    def _reflection(self, state: WorkflowState) -> WorkflowState:
        plans = state["plans"]
        corrections_left = self.settings.max_correction_rounds - (len(plans) - 1)
        try:
            review = self.reflector.review(state["task"], plans, self.today, corrections_left)
        except LLMError as exc:  # includes ValidationFailed
            logger.error("reflection failed: %s", exc)
            self.history.record(state["run_id"], "reflection_failed", error=str(exc))
            self._emit(state, "reflection_failed", str(exc))
            return {"event": "review_failed", "error": str(exc)}
        self.history.record(state["run_id"], "reflection", review=review)
        self._emit(state, "reflection", f"Reflection: {review.status} - {review.assessment}", issues=review.issues)
        update: WorkflowState = {"event": "reviewed", "review": review}
        if review.revised_plan is not None:
            update["pending_plan"] = review.revised_plan.model_copy(update={"round": len(plans)})
            self._emit_plan(state, update["pending_plan"], "reflection")
        return update

    @staticmethod
    def _final_status(state: WorkflowState) -> str:
        if state.get("status") in ("rejected", "completed"):
            return state["status"]
        review = state.get("review")
        return "completed" if review is not None and review.status == "workflow_complete" else "failed"

    def _finalize(self, state: WorkflowState) -> WorkflowState:
        status = state.get("status", "failed")
        if status not in TERMINAL:
            status = "failed"
        plans = state.get("plans", [])
        review = state.get("review")

        if status == "rejected" and not plans:
            gates = state.get("gates", [])
            reason = (
                f"Plan rejected: {'; '.join(gates[-1].concerns) or gates[-1].feedback or 'no reason given'}"
                if gates
                else f"Task rejected by supervisor: {state['decision'].reasoning}"
            )
            report = FinalReport(success=False, summary=reason, actions_taken=[], open_issues=[])
        else:
            try:
                report = self.supervisor.report(state["task"], plans, status, review)
            except LLMError as exc:
                logger.error("report generation failed: %s", exc)
                report = FinalReport(
                    success=status == "completed",
                    summary=f"Run {status}; report generation failed ({exc}).",
                    actions_taken=[s.result.summary for p in plans for s in p.steps if s.result and s.result.success],
                    open_issues=review.issues if review else [],
                )

        if plans:
            save_workflow_run(state["task"], final_plan(plans), report, run_id=state["run_id"], status=status, store=self.memory)
        self.history.record(state["run_id"], "final_report", status=status, report=report)
        return {"status": status, "final_report": report}

    # ================================================================ graph

    def _build_graph(self, checkpointer: BaseCheckpointSaver):
        g = StateGraph(WorkflowState)
        for name, fn in [
            ("supervisor", self._supervisor),
            ("planner", self._planner),
            ("gate", self._gate),
            ("approval", self._approval),
            ("executor", self._executor),
            ("reflection", self._reflection),
            ("finalize", self._finalize),
        ]:
            g.add_node(name, fn)

        def by_next(s: WorkflowState) -> str:
            return s["next"]

        g.add_edge(START, "supervisor")
        g.add_conditional_edges("supervisor", by_next, ["planner", "gate", "reflection", "finalize"])
        g.add_conditional_edges("gate", by_next, ["approval", "executor", "planner", "finalize"])
        g.add_conditional_edges("approval", by_next, ["executor", "planner", "finalize"])
        g.add_conditional_edges("executor", by_next, ["executor", "supervisor"])
        g.add_edge("planner", "supervisor")
        g.add_edge("reflection", "supervisor")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=checkpointer)


# =================================================================== CLI

# Plain ASCII so output survives any console/redirect encoding (e.g. cp1252 on Windows).
TAGS = {
    "run_started": "[start] ", "run_resumed": "[resume]", "intake": "[intake]", "route": "[route] ",
    "plan_created": "[plan]  ", "planning_failed": "[plan!] ", "gate": "[gate]  ", "awaiting_approval": "[wait]  ",
    "approval": "[human] ", "step_started": "  ...   ", "reflection": "[review]", "reflection_failed": "[review!]",
}
STEP_TAGS = {"ok": "  ok    ", "failed": "  FAIL  ", "skipped": "  skip  "}


def print_event(event: ProgressEvent) -> None:
    if event.kind == "step_finished":
        tag = STEP_TAGS.get(event.data.get("status", ""), "  ?     ")
    else:
        tag = TAGS.get(event.kind, "[done]  ")
    print(f"{tag} {event.message}", flush=True)
    for step in event.data.get("steps", []):
        print(f"     {step['step_id']}. {step['tool_name']}: {step['description']}", flush=True)


def print_result(run: WorkflowRun) -> None:
    print(f"\n=== Run {run.run_id} - {run.status.upper()} ===")
    for plan in run.plans:
        print("Original plan:" if plan.round == 0 else f"Round {plan.round}:")
        for s in plan.steps:
            print(f"  [{s.status.value:9}] {s.id}. {s.tool_name}: {s.description}")
    if run.pending_approval:
        req = run.pending_approval
        print(f"\nAwaiting approval (risk {req.gate.risk_level}):")
        for c in req.gate.concerns:
            print(f"  ! {c}")
        for s in req.plan.steps:
            print(f"  {s.id}. {s.tool_name}({json.dumps(s.tool_input)})")
        print(f"\nApprove:  python -m src.orchestrator --approve {run.run_id}")
        print(f"Reject:   python -m src.orchestrator --reject {run.run_id} [--comment \"what to change\"]")
    if run.error:
        print(f"\nError: {run.error}\nResume:  python -m src.orchestrator --resume {run.run_id}")
    report = run.final_report
    if report:
        print(f"\n{report.summary}")
        for a in report.actions_taken:
            print(f"  - {a}")
        if report.open_issues:
            print("Open issues:")
            for i in report.open_issues:
                print(f"  - {i}")
    print(f"\nFull trace: logs/runs/{run.run_id}.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous workflow automation agent")
    parser.add_argument("task", nargs="?", help="Natural-language task to perform")
    parser.add_argument("--workflow", help="Run a named task from data/sample_workflows.json")
    parser.add_argument("--list", action="store_true", help="List sample workflows")
    parser.add_argument("--pending", action="store_true", help="List runs waiting for approval")
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument("--yes", action="store_true", help="Auto-approve plans that need human approval")
    approval.add_argument("--no-wait", action="store_true", help="Pause runs that need approval instead of prompting")
    parser.add_argument("--resume", metavar="RUN_ID", help="Continue an interrupted run")
    parser.add_argument("--approve", metavar="RUN_ID", help="Approve a paused run's plan and continue it")
    parser.add_argument("--reject", metavar="RUN_ID", help="Decline a paused run's plan")
    parser.add_argument("--comment", default="", help="With --reject: feedback; the plan is revised instead of cancelled")
    parser.add_argument("--quiet", action="store_true", help="Don't print live progress")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)
    samples = json.loads(settings.sample_workflows_path.read_text(encoding="utf-8"))

    if args.list:
        for w in samples:
            print(f"{w['name']:20} {w['task']}")
        return 0

    if args.yes:
        approver: Approver | None = AutoApprover()
    elif args.no_wait or not sys.stdin.isatty():
        approver = None  # pause and exit; approve later with --approve
    else:
        approver = ConsoleApprover()
    orchestrator = WorkflowOrchestrator(settings, approver=approver)
    on_event = None if args.quiet else print_event

    if args.pending:
        for r in orchestrator.pending_approvals():
            print(f"{r.run_id}  risk={r.pending_approval.gate.risk_level}  {r.task}")
        return 0

    try:
        if args.approve:
            run = orchestrator.resume(args.approve, ApprovalDecision(approved=True, approver="cli"), on_event=on_event)
        elif args.reject:
            decision = ApprovalDecision(approved=False, approver="cli", comment=args.comment)
            run = orchestrator.resume(args.reject, decision, on_event=on_event)
        elif args.resume:
            run = orchestrator.resume(args.resume, on_event=on_event)
        else:
            task = args.task
            if args.workflow:
                match = next((w for w in samples if w["name"] == args.workflow), None)
                if match is None:
                    parser.error(f"Unknown workflow '{args.workflow}'. Use --list.")
                task = match["task"]
            if not task:
                parser.error("Provide a task or --workflow NAME")
            run = orchestrator.run(task, on_event=on_event)
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_result(run)
    return {"completed": 0, "awaiting_approval": 2}.get(run.status, 1)


if __name__ == "__main__":
    sys.exit(main())
