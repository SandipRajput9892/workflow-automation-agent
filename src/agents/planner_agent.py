"""Planner: turns a natural-language task into an ordered Plan of tool calls.

    "onboard new client Rohan: send welcome email, add to CRM, schedule kickoff call"
        -> Plan(steps=[Step(tool_name="send_email", tool_input={...}), ...])

Claude is given every tool's schema from the ToolRegistry and returns a
PlanDraft via structured output. The draft is then validated in code; if the
response is malformed or any step is invalid (bad JSON in tool_input, unknown
tool, arguments that don't match the tool's schema, forward references), the
planner re-asks Claude with a corrective prompt listing the exact problems,
up to `max_attempts` calls in total.

Try it directly (makes real API calls):
    python -m src.agents.planner_agent "onboard new client Rohan (rohan@nimbus.io, Nimbus Labs): \
send welcome email, add to CRM, schedule kickoff call on 2026-10-05 at 11:00"
"""

from __future__ import annotations

import json
import logging
from datetime import date

from src.agents.base import LLM, ValidationFailed, generate_validated
from src.memory.history_manager import format_past_workflows, get_similar_past_workflows
from src.memory.knowledge_base import format_knowledge
from src.memory.vector_store import WorkflowMemory
from src.schemas import STEP_REF_RE, KnowledgeSnippet, PastWorkflow, Plan, PlanDraft, PlannedStep, Step
from src.tools.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM = """You are the planning agent in a workflow-automation system. Turn the user's task into an ordered \
plan in which every step is exactly one call to one of the available tools. An executor agent will run the \
steps in order.

Guidelines:
- Use exact tool names, and set tool_input_json to a JSON object whose keys and value types match that \
tool's input_schema exactly: every required argument, no extra ones.
- Order steps so that anything a step depends on happens earlier (e.g. create a CRM lead before updating \
its status).
- When an argument needs a value that only exists after an earlier step runs (e.g. the lead_id returned by \
create_lead), use a placeholder string of the form "<lead_id from step N>", where N is an earlier step's id \
and the field name is one the tool's description says it returns. A placeholder can be the whole value or \
appear inside text (e.g. an email body).
- Write complete, ready-to-send content for emails and messages; do not leave text for later.
- Include only steps the task calls for. If the task lacks a detail you need (an email address, a date), \
use your best reasonable value and note the assumption in the step description.
- Number steps 1, 2, 3, ... in execution order.
- Similar past workflows that succeeded may be provided. Reuse their structure and wording where they fit, \
but take every value (names, addresses, dates, IDs) from the current task, never from the examples.
- Company knowledge (policies, SOPs, guidelines) may be provided. Follow it: email tone and sign-off, \
which channel to post in, what a message must or must not include, how to handle existing records. The \
task's explicit instructions win if they conflict; note which guideline you applied in the step description.
- If work_so_far is provided, plan only what is still needed. Never repeat an action that already succeeded \
(no re-sending emails or messages, no re-creating records); reuse IDs from its results instead, and follow \
any supervisor guidance given there."""


class PlanningError(ValidationFailed):
    """No valid plan could be produced within the allowed attempts."""


class PlannerAgent:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        max_steps: int = 15,
        max_attempts: int = 3,
        memory: WorkflowMemory | None = None,
        memory_top_k: int = 3,
    ):
        """`memory`: vector store of past workflows to draw examples from. None
        disables memory lookups (e.g. in tests); memory_top_k=0 does too."""
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.max_attempts = max(1, max_attempts)
        self.memory = memory
        self.memory_top_k = memory_top_k

    def plan(
        self,
        task: str,
        today: str | None = None,
        context: str = "",
        knowledge: list[KnowledgeSnippet] | None = None,
    ) -> Plan:
        """Return a validated Plan for `task`, or raise PlanningError.

        Similar past successful workflows are retrieved from memory and shown to
        Claude as examples. `knowledge`: company policies/SOPs retrieved for the
        task. `context` carries what has happened so far when the supervisor
        asks for a new plan mid-run (execution history, guidance)."""
        parts = [
            f"Today's date: {today or date.today().isoformat()}",
            f"Available tools (Claude tool-use schemas):\n{self.registry.describe()}",
        ]
        past = self.similar_workflows(task)
        if past:
            parts.append(f"<similar_past_workflows>\n{format_past_workflows(past)}\n</similar_past_workflows>")
        if knowledge:
            parts.append(f"<company_knowledge>\n{format_knowledge(knowledge)}\n</company_knowledge>")
        if context:
            parts.append(f"<work_so_far>\n{context}\n</work_so_far>")
        parts.append(f"<task>\n{task}\n</task>")

        def validate(draft: PlanDraft) -> tuple[Plan | None, list[str]]:
            return build_plan(draft.goal, draft.steps, self.registry, self.max_steps)

        try:
            return generate_validated(
                self.llm, SYSTEM, "\n\n".join(parts), PlanDraft, validate, self.max_attempts, what="plan"
            )
        except ValidationFailed as exc:
            raise PlanningError(str(exc), exc.problems) from exc

    def similar_workflows(self, task: str) -> list[PastWorkflow]:
        if self.memory is None or self.memory_top_k <= 0:
            return []
        try:
            return get_similar_past_workflows(task, self.memory_top_k, store=self.memory)
        except Exception as exc:  # memory is an aid; never let it block planning
            logger.warning("memory lookup failed: %s", exc)
            return []


def build_plan(
    goal: str,
    planned: list[PlannedStep],
    registry: ToolRegistry,
    max_steps: int,
    round: int = 0,
) -> tuple[Plan | None, list[str]]:
    """Validate LLM-planned steps and convert them into a Plan.

    Returns (plan, []) or (None, problems). Checks: at least one step, step
    count, ids 1..n in order, tool_input_json is a JSON object, the tool
    exists and the input matches its schema, and placeholders only refer to
    earlier steps. Shared by the planner and the reflection agent."""
    if not planned:
        return None, ["The plan has no steps; every task needs at least one tool call."]
    problems: list[str] = []
    if len(planned) > max_steps:
        problems.append(f"The plan has {len(planned)} steps; the maximum is {max_steps}. Consolidate.")

    ids = [s.id for s in planned]
    if ids != list(range(1, len(ids) + 1)):
        problems.append(f"Step ids must be 1, 2, 3, ... in order; got {ids}.")

    steps: list[Step] = []
    for position, s in enumerate(planned, 1):
        label = f"Step {s.id} ({s.tool_name})"
        try:
            tool_input = json.loads(s.tool_input_json) if s.tool_input_json.strip() else {}
        except json.JSONDecodeError as exc:
            problems.append(f"{label}: tool_input_json is not valid JSON ({exc.msg} at char {exc.pos}).")
            continue
        if not isinstance(tool_input, dict):
            problems.append(f"{label}: tool_input_json must be a JSON object, got {type(tool_input).__name__}.")
            continue

        problems += [f"{label}: {p}" for p in registry.validate_input(s.tool_name, tool_input)]

        for ref in STEP_REF_RE.findall(json.dumps(tool_input)):
            if int(ref) >= position:
                problems.append(f"{label}: placeholder refers to step {ref}, which does not run before this step.")

        steps.append(Step(id=s.id, description=s.description, tool_name=s.tool_name, tool_input=tool_input))

    if problems:
        return None, problems
    return Plan(goal=goal, steps=steps, round=round), []


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from src.agents.base import build_llm
    from src.config import get_settings, setup_logging
    from src.tools.tool_registry import build_default_registry

    parser = argparse.ArgumentParser(description="Generate a plan for a task (no tools are executed).")
    parser.add_argument("task")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)
    from src.memory.history_manager import default_store

    planner = PlannerAgent(
        build_llm(settings),
        build_default_registry(),
        settings.max_steps,
        settings.max_output_attempts,
        memory=default_store(),
        memory_top_k=settings.memory_top_k,
    )
    try:
        plan = planner.plan(args.task)
    except PlanningError as exc:
        print(f"Planning failed: {exc}", file=sys.stderr)
        return 1
    print(plan.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
