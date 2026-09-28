"""Reflection: reviews a fully executed workflow against the original task.

After the executor has run a plan, this agent gets the task and the complete
execution history (every plan round, every tool call with its exact input and
result) and decides:

- workflow_complete: every part of the task was verifiably accomplished.
- revised_plan: something failed, was skipped, produced the wrong result, or
  was never attempted -> a corrective mini-plan of new Steps to execute.
- unrecoverable: the goal can't be reached with the available tools/information.

"No errors" is not the bar: a step can succeed and still be wrong (wrong
recipient, wrong date, email missing requested content), and a plan can run
cleanly while omitting part of the task. Corrective steps are validated exactly
like planner output (see planner_agent.build_plan) and re-requested with a
corrective prompt if invalid.
"""

from __future__ import annotations

import json
from datetime import date

from src.agents.base import LLM, generate_validated
from src.agents.planner_agent import build_plan
from src.schemas import Plan, ReflectionResult, ReflectionReview, StepStatus
from src.tools.tool_registry import ToolRegistry

SYSTEM = """You are the reflection agent in a workflow-automation system. A workflow has just been executed \
on the user's behalf. Decide whether the user's task was actually accomplished, and if not, how to fix it.

How to judge:
- Check every requirement in the task against the tool calls and their results. The goal is achieved only \
if each requirement is backed by a successful tool call with the right inputs.
- "No errors" is not enough. Look for successful calls that did the wrong thing (wrong recipient, date, \
channel, status or company; message content that omits what the task asked for) and for parts of the task \
that no step attempted.
- Failed or skipped steps usually mean the goal was not achieved, unless a later step or correction round \
accomplished the same thing.

When the goal was not achieved and more tool calls can fix it, return needs_correction with \
corrective_steps:
- Only fix what is wrong or missing. Do not repeat actions that already succeeded; in particular, never \
re-send an email or message, or re-create a record, that already went through.
- Use literal values from the execution history (e.g. an existing LEAD-0001) rather than recreating things.
- Each step is one tool call; tool_input_json must match the tool's input_schema exactly. Placeholders of \
the form "<field from step N>" may refer only to earlier steps in your corrective list.

Return unrecoverable when no available tool call can fix the problem (e.g. required information is \
missing from the task), and explain what a human needs to do in the assessment."""

ROUND_LABEL = {0: "Original plan"}


class ReflectionAgent:
    def __init__(self, llm: LLM, registry: ToolRegistry, max_steps: int = 15, max_attempts: int = 3):
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.max_attempts = max(1, max_attempts)

    def review(self, task: str, plans: list[Plan], today: str | None = None, corrections_left: int = 1) -> ReflectionResult:
        """Review the executed `plans` (original first, then any corrective
        rounds) against `task`. Raises ValidationFailed if Claude can't produce a
        consistent, valid review within the allowed attempts."""
        next_round = (plans[-1].round + 1) if plans else 1
        parts = [
            f"Today's date: {today or date.today().isoformat()}",
            f"Available tools (Claude tool-use schemas):\n{self.registry.describe()}",
            f"<task>\n{task}\n</task>",
            f"<execution_history>\n{format_execution_history(plans)}\n</execution_history>",
            f"Correction rounds remaining: {corrections_left}"
            + ("" if corrections_left > 0 else " (if the goal was not achieved, the verdict must be unrecoverable)"),
        ]

        def validate(review: ReflectionReview) -> tuple[ReflectionResult | None, list[str]]:
            problems: list[str] = []
            if review.verdict == "workflow_complete" and not review.goal_achieved:
                problems.append("verdict is workflow_complete but goal_achieved is false.")
            if review.verdict == "needs_correction":
                if review.goal_achieved:
                    problems.append("verdict is needs_correction but goal_achieved is true.")
                if corrections_left <= 0:
                    problems.append("No correction rounds remain; use unrecoverable instead of needs_correction.")
                if not review.corrective_steps:
                    problems.append("verdict is needs_correction but corrective_steps is empty.")
                else:
                    plan, step_problems = build_plan(
                        f"Correct: {task}", review.corrective_steps, self.registry, self.max_steps, round=next_round
                    )
                    problems += [f"corrective_steps: {p}" for p in step_problems]
                    if not problems:
                        return ReflectionResult(
                            status="revised_plan", assessment=review.assessment, issues=review.issues, revised_plan=plan
                        ), []
            if problems:
                return None, problems

            status = "workflow_complete" if review.verdict == "workflow_complete" else "unrecoverable"
            return ReflectionResult(status=status, assessment=review.assessment, issues=review.issues), []

        return generate_validated(
            self.llm, SYSTEM, "\n\n".join(parts), ReflectionReview, validate, self.max_attempts, what="review"
        )


def format_execution_history(plans: list[Plan]) -> str:
    """Every plan round with each step's status and exact tool calls/results."""
    if not plans:
        return "(nothing was executed)"
    blocks = []
    for plan in plans:
        label = ROUND_LABEL.get(plan.round, f"Corrective plan (round {plan.round})")
        lines = [f"== {label} ==  goal: {plan.goal}"]
        for step in plan.steps:
            lines.append(f"Step {step.id} [{step.status.value}] {step.tool_name}: {step.description}")
            result = step.result
            if result is None:
                if step.status == StepStatus.SKIPPED:
                    lines.append("  (not executed)")
                continue
            if result.skipped:
                lines.append(f"  skipped: {result.error}")
            for n, call in enumerate(result.tool_calls, 1):
                outcome = call.result.model_dump(exclude={"tool"}, exclude_none=True)
                lines.append(f"  call {n}: input={json.dumps(call.tool_input)} -> {json.dumps(outcome, default=str)}")
            if result.error and not result.skipped:
                lines.append(f"  final error: {result.error}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

