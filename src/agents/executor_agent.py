"""Executor: runs a Plan's steps in order by calling tools from the registry.

For each step:
1. Resolve placeholders like "<lead_id from step 1>" from earlier steps' results.
   If a referenced step did not complete, the step is SKIPPED (not attempted).
2. Call the step's tool with its input and capture the ToolResult.
3. On failure, ask Claude for adjusted input (given the error, the tool's
   schema and earlier results) and retry, up to `max_retries` times.
4. Record the ExecutionResult on the step and set its status.

The happy path involves no LLM call at all: tools are invoked directly with the
planner's input. Claude is only consulted to repair a failed call.

Every action (tool call, input adjustment, skipped step) is appended to the
audit log (logs/audit_log.json).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from src.agents.base import LLM, LLMError
from src.memory.history_manager import AuditLog
from src.schemas import (
    PLACEHOLDER_RE,
    STEP_REF_RE,
    ExecutionResult,
    InputAdjustment,
    Plan,
    Step,
    StepStatus,
    ToolCall,
)
from src.tools.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

ADJUST_SYSTEM = """You are the execution agent in a workflow-automation system. A tool call from the plan \
just failed. Produce corrected input for the SAME tool so the retry succeeds.

- Fix exactly what the error points to (format, invalid value, wrong ID, unresolved placeholder). Keep \
everything else unchanged, including message content.
- Take IDs and other values from the results of earlier steps; never invent them.
- If no change to the input can make the call succeed (e.g. the record already exists, or needed \
information is not available anywhere), set can_fix to false and explain why in the rationale."""


class _Blocked(Exception):
    """The step depends on a step that did not complete; it cannot run."""


class ExecutorAgent:
    def __init__(self, llm: LLM, registry: ToolRegistry, audit: AuditLog, max_retries: int = 1):
        self.llm = llm
        self.registry = registry
        self.audit = audit
        self.max_retries = max_retries

    # ------------------------------------------------------------------ public

    def execute_plan(self, plan: Plan, task: str = "", run_id: str | None = None, today: str | None = None) -> Plan:
        """Execute every pending step in order. Returns a new Plan with each
        step's status and result filled in; the input plan is not modified.
        A failed step does not stop the run: later steps that don't depend on
        it still execute, and those that do are skipped."""
        plan = plan.model_copy(deep=True)
        for idx, step in enumerate(plan.steps):
            if step.status != StepStatus.PENDING:
                continue  # already executed (e.g. resuming a partially run plan)
            plan = apply_result(plan, idx, self.execute_step(plan, idx, task, run_id=run_id, today=today))
        return plan

    def execute_step(
        self,
        plan: Plan,
        idx: int,
        task: str = "",
        *,
        run_id: str | None = None,
        today: str | None = None,
    ) -> ExecutionResult:
        """Execute plan.steps[idx], retrying with adjusted input on failure."""
        step = plan.steps[idx]
        today = today or date.today().isoformat()
        calls: list[ToolCall] = []

        try:
            tool_input, error = self._resolve(plan, step, step.tool_input)
        except _Blocked as exc:
            self._audit("step_skipped", plan, step, None, {"reason": str(exc)}, 0, run_id)
            logger.warning("step %d skipped: %s", step.id, exc)
            return ExecutionResult(step_id=step.id, success=False, skipped=True, summary=f"Skipped: {exc}", error=str(exc))

        if error is None:
            call = self._call(plan, step, tool_input, 1, run_id)
            calls.append(call)
            if call.result.success:
                return self._result(step, calls)
            error = call.result.error

        for _ in range(self.max_retries):
            adjusted, reason = self._adjust(task, today, plan, step, tool_input, error or "")
            next_call = len(calls) + 1
            self._audit(
                "input_adjusted", plan, step, adjusted, {"retrying": adjusted is not None, "rationale": reason}, next_call, run_id
            )
            if adjusted is None:
                error = f"{error} (not retried: {reason})"
                break
            tool_input = adjusted
            call = self._call(plan, step, tool_input, next_call, run_id)
            calls.append(call)
            if call.result.success:
                return self._result(step, calls)
            error = call.result.error

        return self._result(step, calls, error=error)

    # --------------------------------------------------------------- internals

    def _call(self, plan: Plan, step: Step, tool_input: dict[str, Any], attempt: int, run_id: str | None) -> ToolCall:
        result = self.registry.execute(step.tool_name, tool_input)
        self._audit("tool_call", plan, step, tool_input, result, attempt, run_id)
        return ToolCall(tool_name=step.tool_name, tool_input=tool_input, result=result)

    @staticmethod
    def _result(step: Step, calls: list[ToolCall], error: str | None = None) -> ExecutionResult:
        if error is None:
            data = calls[-1].result.data or {}
            summary = f"{step.tool_name} succeeded: {json.dumps(data, default=str)}"
            if len(calls) > 1:
                retries = len(calls) - 1
                summary += f" (after {retries} {'retry' if retries == 1 else 'retries'})"
            return ExecutionResult(step_id=step.id, success=True, summary=summary, tool_calls=calls)
        return ExecutionResult(
            step_id=step.id, success=False, summary=f"{step.tool_name} failed: {error}", tool_calls=calls, error=error
        )

    def _audit(
        self, action: str, plan: Plan, step: Step, tool_input: Any, result: Any, attempt: int, run_id: str | None
    ) -> None:
        self.audit.record(
            action,
            step_id=step.id,
            step=step.description,
            tool=step.tool_name,
            input=tool_input,
            result=result,
            attempt=attempt,
            plan_round=plan.round,
            run_id=run_id,
        )

    # ---------------------------------------------------- placeholder handling

    def _resolve(self, plan: Plan, step: Step, tool_input: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Substitute "<field from step N>" placeholders with values from step N's
        result. Returns (input, error); error is set if something could not be
        resolved but might be fixable by adjustment. Raises _Blocked if a
        referenced step did not complete."""
        problems: list[str] = []
        by_id = {s.id: s for s in plan.steps}

        def lookup(field: str, ref: int) -> Any:
            source = by_id.get(ref)
            if source is None or ref >= step.id:
                problems.append(f"placeholder refers to step {ref}, which does not run before this step")
                return None
            output = source.result.output() if source.result else None
            if source.status != StepStatus.COMPLETED or output is None:
                raise _Blocked(f"depends on step {ref} ({source.tool_name}), which did not complete")
            if field not in output:
                problems.append(f"step {ref} result has no field '{field}' (available: {', '.join(output) or 'none'})")
                return None
            return output[field]

        def substitute(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            if not isinstance(value, str):
                return value
            whole = PLACEHOLDER_RE.fullmatch(value.strip())
            if whole:  # keep the referenced value's type
                found = lookup(whole.group(1), int(whole.group(2)))
                return value if found is None else found

            def repl(m: Any) -> str:
                found = lookup(m.group(1), int(m.group(2)))
                return m.group(0) if found is None else str(found)

            return PLACEHOLDER_RE.sub(repl, value)

        resolved = substitute(tool_input)
        leftover = STEP_REF_RE.findall(json.dumps(resolved))
        if leftover and not problems:
            problems.append("input contains step references that are not in the '<field from step N>' form")
        return resolved, ("Unresolved placeholders: " + "; ".join(problems)) if problems else None

    # ------------------------------------------------------- input adjustment

    def _adjust(
        self,
        task: str,
        today: str,
        plan: Plan,
        step: Step,
        failed_input: dict[str, Any],
        error: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Ask Claude for corrected input. Returns (input, rationale), or
        (None, reason) if Claude says it can't be fixed or the answer is unusable."""
        earlier = "\n".join(
            f"- Step {s.id} ({s.tool_name}) [{s.status.value}]: {json.dumps(s.result.output(), default=str)}"
            for s in plan.steps
            if s.id < step.id and s.result is not None
        )
        parts = [
            f"Today's date: {today}",
            f"Overall goal: {task or plan.goal}",
            f"Tool schema:\n{json.dumps(self.registry.schema(step.tool_name), indent=2)}",
            f"Results of earlier steps:\n{earlier or '(none)'}",
            f"Step {step.id}: {step.description}",
            f"Failed input:\n{json.dumps(failed_input, indent=2, default=str)}",
            f"Error: {error}",
        ]

        try:
            adjustment = self.llm.structured(ADJUST_SYSTEM, "\n\n".join(parts), InputAdjustment)
        except LLMError as exc:
            return None, f"could not get adjusted input: {exc}"
        if not adjustment.can_fix:
            return None, adjustment.rationale

        try:
            adjusted = json.loads(adjustment.tool_input_json)
        except json.JSONDecodeError as exc:
            return None, f"adjusted input is not valid JSON ({exc.msg})"
        if not isinstance(adjusted, dict):
            return None, "adjusted input is not a JSON object"
        try:
            adjusted, problem = self._resolve(plan, step, adjusted)
        except _Blocked as exc:
            return None, str(exc)
        if problem:
            return None, problem
        invalid = self.registry.validate_input(step.tool_name, adjusted)
        if invalid:
            return None, "adjusted input is invalid: " + "; ".join(invalid)
        logger.info("step %d: retrying with adjusted input (%s)", step.id, adjustment.rationale)
        return adjusted, adjustment.rationale


def apply_result(plan: Plan, idx: int, result: ExecutionResult) -> Plan:
    """Copy of `plan` with step `idx` carrying `result` and the matching status."""
    status = StepStatus.SKIPPED if result.skipped else StepStatus.COMPLETED if result.success else StepStatus.FAILED
    steps = list(plan.steps)
    steps[idx] = steps[idx].model_copy(update={"result": result, "status": status})
    return plan.model_copy(update={"steps": steps})
