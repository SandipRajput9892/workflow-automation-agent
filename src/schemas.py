"""Shared data models.

Two kinds of models live here:
- Domain models (Step, Plan, ToolResult, ExecutionResult, WorkflowRun) used
  throughout the system.
- LLM output models (SupervisorDecision, PlanDraft, InputAdjustment,
  ReflectionReview, FinalReport) that Claude fills via structured outputs. These avoid free-form dict fields,
  which structured outputs cannot express; PlanDraft carries tool input as a
  JSON string and is converted into a Plan.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Tool + execution results
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """Structured success/failure result returned by every tool function."""

    success: bool
    tool: str
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    @classmethod
    def ok(cls, tool: str, **data: Any) -> "ToolResult":
        return cls(success=True, tool=tool, data=data)

    @classmethod
    def fail(cls, tool: str, error: str) -> "ToolResult":
        return cls(success=False, tool=tool, error=error)


class ToolCall(BaseModel):
    tool_name: str
    tool_input: dict[str, Any]
    result: ToolResult


class ExecutionResult(BaseModel):
    """Outcome of one attempt at executing a Step."""

    step_id: int
    success: bool
    summary: str = Field(description="Executor's summary of what happened.")
    tool_calls: list[ToolCall] = Field(default_factory=list)
    error: Optional[str] = None
    skipped: bool = False  # not attempted because a step it depends on did not complete
    finished_at: datetime = Field(default_factory=utcnow)

    def output(self) -> dict[str, Any] | None:
        """Data returned by the last successful tool call, if any."""
        for call in reversed(self.tool_calls):
            if call.result.success:
                return call.result.data or {}
        return None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


# A value that only exists after an earlier step runs, e.g. "<lead_id from step 1>".
# PLACEHOLDER_RE is the canonical form the executor resolves (group 1 = field in
# that step's tool result data, group 2 = step id). STEP_REF_RE is looser and
# catches any reference to a step, including non-canonical ones.
PLACEHOLDER_RE = re.compile(r"<\s*([A-Za-z_]\w*)\s+from\s+step\s+(\d+)\s*>", re.IGNORECASE)
STEP_REF_RE = re.compile(r"<[^<>]*\bstep\s+(\d+)\s*>", re.IGNORECASE)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Step(BaseModel):
    id: int
    description: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    result: Optional[ExecutionResult] = None


class Plan(BaseModel):
    goal: str
    steps: list[Step]
    # 0 = the planner's original plan; 1, 2, ... = corrective plans from the
    # reflection agent. Step ids restart at 1 in every plan.
    round: int = 0


# ---------------------------------------------------------------------------
# Workflow run
# ---------------------------------------------------------------------------

# awaiting_approval: paused until a human approves the pending plan (resumable).
# interrupted: stopped by an unexpected error mid-run (resumable from the last checkpoint).
RunStatus = Literal["running", "completed", "failed", "rejected", "awaiting_approval", "interrupted"]


class FinalReport(BaseModel):
    success: bool
    summary: str
    actions_taken: list[str]
    open_issues: list[str]


class WorkflowRun(BaseModel):
    """Complete record of one task, from intake to final report."""

    run_id: str
    task: str
    status: RunStatus = "running"
    plans: list[Plan] = Field(default_factory=list)  # executed plans in order (original, then corrections/replans)
    review: Optional["ReflectionResult"] = None  # the reflection agent's last verdict
    gates: list["GateResult"] = Field(default_factory=list)  # supervisor's pre-execution review of each plan
    routes: list["RouteRecord"] = Field(default_factory=list)  # supervisor's routing decisions, in order
    pending_approval: Optional["ApprovalRequest"] = None  # set while status == "awaiting_approval"
    error: Optional[str] = None  # set when status == "interrupted"
    final_report: Optional[FinalReport] = None
    knowledge: list["KnowledgeSnippet"] = Field(default_factory=list)  # company knowledge retrieved at intake
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None

    @property
    def plan(self) -> Optional[Plan]:
        """The original plan."""
        return self.plans[0] if self.plans else None

    @property
    def results(self) -> list[ExecutionResult]:
        """Every step result across all plans, in execution order."""
        return [s.result for p in self.plans for s in p.steps if s.result is not None]

    @property
    def correction_rounds(self) -> int:
        return max(len(self.plans) - 1, 0)


class PastWorkflow(BaseModel):
    """A workflow retrieved from long-term memory."""

    run_id: str
    task: str
    plan: Plan  # the final plan: the tool calls that were actually made
    outcome: str
    success: bool
    status: str
    distance: float  # cosine distance to the query (0 = identical)


class KnowledgeSnippet(BaseModel):
    """A passage of company knowledge (policy, SOP, guideline) retrieved for a task."""

    source: str  # file name in the knowledge folder, e.g. email_guidelines.md
    title: str  # the document's title
    section: str = ""  # heading of the section the passage comes from
    text: str
    distance: float  # cosine distance to the query (0 = identical)


# ---------------------------------------------------------------------------
# LLM structured outputs
# ---------------------------------------------------------------------------


class SupervisorDecision(BaseModel):
    """Supervisor's intake review of a task before any planning happens."""

    approved: bool = Field(description="Whether the task is safe, clear and achievable with the available tools.")
    risk_level: Literal["low", "medium", "high"]
    normalized_task: str = Field(description="The task restated precisely, with any implicit details made explicit.")
    required_tools: list[str] = Field(description="Tool names likely needed.")
    reasoning: str


class PlannedStep(BaseModel):
    id: int = Field(description="1-based step number, unique within the plan.")
    description: str = Field(description="What this step must accomplish, specific enough to execute on its own.")
    tool_name: str = Field(description="Exact name of the tool this step calls.")
    tool_input_json: str = Field(
        description=(
            "JSON object of arguments for the tool, matching its input schema. Where a value depends on an "
            'earlier step\'s output, use a placeholder like "<lead_id from step 1>".'
        )
    )


class PlanDraft(BaseModel):
    """Raw planner output; validated and converted to a Plan by PlannerAgent."""

    goal: str
    steps: list[PlannedStep]


class InputAdjustment(BaseModel):
    """Executor's corrected tool input after a failed call."""

    can_fix: bool = Field(description="False if no change to the input could make this call succeed.")
    tool_input_json: str = Field(description="Corrected JSON object of arguments for the same tool. '{}' if can_fix is false.")
    rationale: str = Field(description="What was wrong and what was changed.")


class ReflectionReview(BaseModel):
    """Reflection agent's review of a fully executed workflow (LLM output)."""

    goal_achieved: bool = Field(
        description="True only if every part of the task was actually accomplished, as shown by the tool results."
    )
    assessment: str = Field(description="Brief judgment of the outcome against the task, citing the evidence.")
    issues: list[str] = Field(
        description="Each failed/skipped step, unexpected or wrong result, and part of the task never done. Empty if none."
    )
    verdict: Literal["workflow_complete", "needs_correction", "unrecoverable"] = Field(
        description=(
            "workflow_complete: goal achieved, nothing to fix. needs_correction: fixable with more tool calls; "
            "provide corrective_steps. unrecoverable: cannot be fixed with the available tools or information."
        )
    )
    corrective_steps: list[PlannedStep] = Field(
        description=(
            "Only for needs_correction: new steps that fix the issues without repeating work that already "
            "succeeded. Placeholders may only refer to steps in this list; use literal values (IDs etc.) from "
            "the execution history. Empty for the other verdicts."
        )
    )


class ReflectionResult(BaseModel):
    """What the reflection agent returns to the orchestrator."""

    status: Literal["workflow_complete", "revised_plan", "unrecoverable"]
    assessment: str
    issues: list[str] = Field(default_factory=list)
    revised_plan: Optional[Plan] = None  # set when status == "revised_plan"


# ---------------------------------------------------------------------------
# Supervisor: plan gate and routing
# ---------------------------------------------------------------------------

RiskLevel = Literal["low", "medium", "high"]
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class PlanAssessment(BaseModel):
    """Supervisor's review of a plan before it executes (LLM output)."""

    verdict: Literal["approve", "revise", "reject"] = Field(
        description=(
            "approve: the plan does what the task asks and nothing more. revise: fixable problems; explain in "
            "feedback. reject: the plan (or the task behind it) should not be carried out at all."
        )
    )
    risk_level: RiskLevel = Field(description="Risk of executing this plan, judged by its real-world side effects.")
    concerns: list[str] = Field(description="Specific problems or risks, each tied to a step where possible. Empty if none.")
    feedback: str = Field(description="For revise: concrete instructions for the planner. Empty otherwise.")


class PolicyFinding(BaseModel):
    """Result of a deterministic policy check on a plan step."""

    severity: Literal["info", "high_risk", "block"]
    step_id: Optional[int] = None
    message: str


class ApprovalDecision(BaseModel):
    """A human's answer to an approval request."""

    approved: bool
    approver: str = "human"
    comment: str = ""


class GateResult(BaseModel):
    """Outcome of the supervisor's pre-execution gate for one plan."""

    plan_round: int
    decision: Literal["approved", "revise", "rejected"]
    risk_level: RiskLevel
    concerns: list[str] = Field(default_factory=list)
    feedback: str = ""
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    human_approval: Optional[ApprovalDecision] = None  # set if a human was asked


class ApprovalRequest(BaseModel):
    """A plan waiting for a human decision (the run is paused until it is answered)."""

    plan: Plan
    gate: GateResult
    requested_at: datetime = Field(default_factory=utcnow)


AgentName = Literal["planner", "executor", "reflection", "finish"]


class RouteDecision(BaseModel):
    """Supervisor's choice of which agent acts next (LLM output)."""

    next: AgentName
    reasoning: str
    guidance: str = Field(description="Instructions for the chosen agent (e.g. what the planner should do differently). Empty if none.")


class RouteRecord(BaseModel):
    """A routing decision as recorded on the run."""

    after: str  # the event the supervisor reacted to, e.g. "reviewed"
    options: list[AgentName]
    next: AgentName
    reasoning: str
    guidance: str = ""
    by_llm: bool  # False when only one move was allowed and no Claude call was needed


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class ProgressEvent(BaseModel):
    """A live progress update emitted while a workflow runs."""

    kind: str  # e.g. run_started, plan_created, gate, awaiting_approval, step_finished, reflection, run_finished
    message: str
    run_id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
    run: Optional["WorkflowRun"] = None  # only on the final event of a run() / resume() call


class WorkflowState(TypedDict, total=False):
    run_id: str
    task: str
    started_at: datetime
    status: RunStatus
    decision: SupervisorDecision  # intake review
    event: str  # what just happened; the supervisor routes on it
    error: str  # detail for failure events
    next: str  # routing decision: an AgentName, or "gate" / "approval" / "finalize"
    guidance: str  # supervisor instructions for the next agent
    pending_plan: Optional[Plan]  # proposed plan awaiting the gate / execution
    plans: list[Plan]  # executed plans, in order
    review: ReflectionResult  # latest reflection verdict
    gates: list[GateResult]
    routes: list[RouteRecord]
    pending_approval: Optional[ApprovalRequest]  # set while waiting for a human
    plan_requests: int  # times the planner has been invoked
    knowledge: list[KnowledgeSnippet]  # company knowledge retrieved at intake
    final_report: FinalReport


WorkflowRun.model_rebuild()
ProgressEvent.model_rebuild()
