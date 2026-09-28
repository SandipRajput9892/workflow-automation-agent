"""Supervisor: intake gate, router between agents, pre-execution safety gate, final report.

Responsibilities
1. Intake (`review`): approve or reject the task, rate its risk, restate it precisely.
2. Routing (`allowed_moves` + `route`): after every agent finishes, decide who acts
   next. Code decides which moves are *allowed* (guardrails and budgets); Claude
   chooses among them only when there is a real choice (e.g. execute the
   reflection agent's corrective plan vs. replan from scratch vs. stop). When
   exactly one move is allowed, no Claude call is made.
3. Safety gate (`review_plan`): every plan (original, corrective, or replanned)
   passes three layers before any tool runs:
     a. deterministic policy checks: repeats of actions that already succeeded
        (blocking), bulk recipients / bulk messaging (high risk), external
        recipients (info);
     b. Claude's review of the plan against the task (approve / revise / reject,
        risk level);
     c. human approval, depending on settings.approval_mode (`needs_human`).
        The orchestrator asks the human (or pauses the run until they answer)
        and records the answer with `apply_approval`.
4. Final report (`report`).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Protocol

from src.agents.base import LLM, ValidationFailed, generate_validated
from src.agents.reflection_agent import format_execution_history
from src.config import Settings
from src.schemas import (
    RISK_ORDER,
    AgentName,
    ApprovalDecision,
    FinalReport,
    GateResult,
    Plan,
    PlanAssessment,
    PolicyFinding,
    ReflectionResult,
    RiskLevel,
    RouteDecision,
    RouteRecord,
    SupervisorDecision,
    WorkflowState,
)
from src.tools.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

INTAKE_SYSTEM = """You are the supervisor of an autonomous workflow-automation system that acts on a user's \
behalf across email, calendar, CRM and Slack.

Review each incoming task before any work starts:
- Approve it when it is a legitimate business workflow achievable with the available tools.
- Reject it when it is unsafe, clearly abusive (spam, harassment, data exfiltration), or needs capabilities \
the tools do not provide. Explain why in the reasoning.
- Rate risk by blast radius: internal notifications are low; messaging external contacts or changing CRM \
records is medium; bulk messaging or irreversible changes are high.
- Restate the task precisely in normalized_task, resolving relative dates against today's date."""

GATE_SYSTEM = """You are the supervisor of an autonomous workflow-automation system. A plan is about to be \
executed; every step has real side effects (emails and Slack messages are sent, calendar events and CRM \
records are created or changed). Review it before it runs.

- approve: every step serves the task, recipients/dates/IDs/content match what the task asked for, and \
nothing beyond the task is done.
- revise: the plan is close but has fixable problems: a wrong or invented value, content that contradicts \
the task, a missing or unnecessary step, or a step that repeats something already done. Put precise \
instructions in feedback.
- reject: the plan should not run at all (e.g. it would contact people the task never mentions, or it \
turns out the task itself is harmful).

Rate risk by real-world impact: internal notifications are low; messages to external people and CRM \
changes are medium; bulk messaging, many recipients, or hard-to-undo changes are high. Automated policy \
findings are included; take them into account but do not just restate them."""

ROUTE_SYSTEM = """You are the supervisor of an autonomous workflow-automation system. You coordinate a \
planner (writes a plan of tool calls), an executor (runs a plan), and a reflection agent (checks the \
result against the task and proposes corrections). Choose which one acts next, from the allowed options \
only.

- executor: run the pending plan (it will pass your safety review first). Prefer this when the pending \
corrective plan is a sensible, targeted fix.
- planner: write a new plan from scratch for the remaining work. Prefer this when the pending plan (or \
the approach so far) is fundamentally wrong. Give the planner specific guidance.
- finish: stop now. Choose this when the task is done, cannot be completed with the available tools and \
information, or further attempts would likely repeat the same failure.

Never repeat an approach that has already failed the same way."""

REPORT_SYSTEM = """You are the supervisor of an autonomous workflow-automation system. Write the final report \
for a finished run based strictly on the execution log. Do not claim any action that the log does not show \
succeeding. List anything left undone or uncertain as an open issue."""


OPTION_HELP: dict[str, str] = {
    "planner": "write a new plan from scratch for the remaining work",
    "executor": "execute the pending plan (after safety review)",
    "reflection": "review the executed work against the task",
    "finish": "stop and write the final report",
}


# ---------------------------------------------------------------------------
# Human approval
# ---------------------------------------------------------------------------


class Approver(Protocol):
    """Called when a plan needs human sign-off. Denying with a comment sends the
    plan back to the planner with the comment as guidance. When the orchestrator
    has no approver, it pauses the run instead and waits for resume()."""

    def __call__(self, task: str, plan: Plan, gate: GateResult) -> ApprovalDecision: ...


class AutoApprover:
    """Approves everything (e.g. `--yes` on the CLI, or trusted batch jobs)."""

    def __call__(self, task: str, plan: Plan, gate: GateResult) -> ApprovalDecision:
        return ApprovalDecision(approved=True, approver="auto")


class DenyApprover:
    """Denies everything. The safe default when nobody is around to ask."""

    def __call__(self, task: str, plan: Plan, gate: GateResult) -> ApprovalDecision:
        return ApprovalDecision(approved=False, approver="default-deny", comment="")


class ConsoleApprover:
    """Asks on the terminal. Denies automatically if stdin is not interactive."""

    def __call__(self, task: str, plan: Plan, gate: GateResult) -> ApprovalDecision:
        if not sys.stdin.isatty():
            return ApprovalDecision(approved=False, approver="console", comment="")
        print("\n" + "=" * 70)
        print(f"APPROVAL REQUIRED  (risk: {gate.risk_level})")
        print(f"Task: {task}")
        for c in gate.concerns:
            print(f"  ! {c}")
        print("Plan:")
        for s in plan.steps:
            print(f"  {s.id}. {s.tool_name}({json.dumps(s.tool_input)})\n     {s.description}")
        answer = input("Approve? [y = run it / n = cancel / anything else = feedback for a revised plan]: ").strip()
        if answer.lower() in ("y", "yes"):
            return ApprovalDecision(approved=True, approver="console")
        if answer.lower() in ("", "n", "no"):
            return ApprovalDecision(approved=False, approver="console")
        return ApprovalDecision(approved=False, approver="console", comment=answer)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class SupervisorAgent:
    def __init__(self, llm: LLM, registry: ToolRegistry, settings: Settings):
        self.llm = llm
        self.registry = registry
        self.settings = settings

    # ---------------------------------------------------------------- intake

    def review(self, task: str, today: str) -> SupervisorDecision:
        prompt = (
            f"Today's date: {today}\n\nAvailable tools (Claude tool-use schemas):\n{self.registry.describe()}\n\n"
            f"<task>\n{task}\n</task>"
        )
        return self.llm.structured(INTAKE_SYSTEM, prompt, SupervisorDecision)

    # --------------------------------------------------------------- routing

    def allowed_moves(self, state: WorkflowState) -> tuple[list[AgentName], str]:
        """The moves allowed after the latest event, and a description of the
        situation. This is where the guardrails live: e.g. never execute
        without a pending plan, never exceed the correction or planning budget."""
        s = self.settings
        event = state.get("event", "")
        plans = state.get("plans", [])
        review = state.get("review")
        can_execute = len(plans) < 1 + s.max_correction_rounds
        can_plan = can_execute and state.get("plan_requests", 0) < s.max_plan_requests

        if event == "planned":
            return ["executor"], "The planner produced a plan."
        if event == "planning_failed":
            return (["planner"] if can_plan else []) + ["finish"], f"Planning failed: {state.get('error', '')}"
        if event == "executed":
            return ["reflection"], "A plan finished executing."
        if event == "reviewed" and review is not None:
            if review.status == "workflow_complete":
                return ["finish"], "The reflection agent confirmed the task is complete."
            situation = f"Reflection verdict: {review.status}. {review.assessment}"
            if review.status == "revised_plan":
                options: list[AgentName] = (["executor"] if can_execute else []) + (["planner"] if can_plan else [])
                return options + ["finish"], situation + " A corrective plan is pending."
            return (["planner"] if can_plan else []) + ["finish"], situation
        if event == "review_failed":
            return ["finish"], f"Reflection failed: {state.get('error', '')}"
        return ["finish"], f"Unexpected event '{event}'."

    def route(self, task: str, state: WorkflowState, options: list[AgentName], situation: str) -> RouteRecord:
        """Pick the next agent from `options`. Only calls Claude if there is a choice."""
        event = state.get("event", "")
        if len(options) == 1:
            return RouteRecord(after=event, options=options, next=options[0], reasoning=situation, by_llm=False)

        s = self.settings
        plans = state.get("plans", [])
        pending = state.get("pending_plan")
        parts = [
            f"<task>\n{task}\n</task>",
            f"<execution_history>\n{format_execution_history(plans)}\n</execution_history>",
            f"Situation: {situation}",
        ]
        review = state.get("review")
        if review is not None and review.issues:
            parts.append("Open issues:\n" + "\n".join(f"- {i}" for i in review.issues))
        if pending is not None and "executor" in options:
            parts.append(
                "Pending plan:\n"
                + "\n".join(f"{st.id}. {st.tool_name}({json.dumps(st.tool_input)}): {st.description}" for st in pending.steps)
            )
        parts.append(
            f"Budget: {len(plans)} of {1 + s.max_correction_rounds} plan executions used; "
            f"{state.get('plan_requests', 0)} of {s.max_plan_requests} planner calls used."
        )
        parts.append("Allowed options:\n" + "\n".join(f"- {o}: {OPTION_HELP[o]}" for o in options))

        def validate(d: RouteDecision) -> tuple[RouteDecision | None, list[str]]:
            if d.next not in options:
                return None, [f"'{d.next}' is not an allowed option here; choose one of {options}."]
            return d, []

        try:
            decision = generate_validated(
                self.llm, ROUTE_SYSTEM, "\n\n".join(parts), RouteDecision, validate, s.max_output_attempts, what="decision"
            )
        except ValidationFailed as exc:
            logger.warning("routing fell back to 'finish': %s", exc)
            return RouteRecord(
                after=event, options=options, next="finish", reasoning=f"Routing failed ({exc}); stopping.", by_llm=True
            )
        return RouteRecord(
            after=event,
            options=options,
            next=decision.next,
            reasoning=decision.reasoning,
            guidance=decision.guidance,
            by_llm=True,
        )

    # ---------------------------------------------------------- safety gate

    def check_policy(self, plan: Plan, executed: list[Plan]) -> list[PolicyFinding]:
        """Deterministic checks that don't need an LLM."""
        s = self.settings
        findings: list[PolicyFinding] = []
        internal = {d.lower() for d in s.internal_domains}

        done = {
            (c.tool_name, _canonical(c.tool_input)): (p.round, st.id)
            for p in executed
            for st in p.steps
            if st.result
            for c in st.result.tool_calls
            if c.result.success
        }

        outbound = 0
        for step in plan.steps:
            key = (step.tool_name, _canonical(step.tool_input))
            if key in done:
                rnd, sid = done[key]
                findings.append(PolicyFinding(
                    severity="block",
                    step_id=step.id,
                    message=f"Step {step.id} repeats {step.tool_name} exactly as already done successfully "
                    f"(round {rnd}, step {sid}); it must not run again.",
                ))

            recipients: list[str] = []
            if step.tool_name == "send_email":
                outbound += 1
                recipients = [a.strip() for a in str(step.tool_input.get("to", "")).split(",") if a.strip()]
            elif step.tool_name == "create_event":
                recipients = [a for a in step.tool_input.get("attendees", []) if isinstance(a, str)]
            elif step.tool_name == "send_message":
                outbound += 1

            if len(recipients) > s.bulk_recipient_threshold:
                findings.append(PolicyFinding(
                    severity="high_risk",
                    step_id=step.id,
                    message=f"Step {step.id} ({step.tool_name}) has {len(recipients)} recipients "
                    f"(bulk threshold {s.bulk_recipient_threshold}).",
                ))
            if internal:
                external = [r for r in recipients if "@" in r and r.rsplit("@", 1)[1].lower() not in internal]
                if external:
                    findings.append(PolicyFinding(
                        severity="info",
                        step_id=step.id,
                        message=f"Step {step.id} ({step.tool_name}) contacts external address(es): {', '.join(external)}.",
                    ))

        if outbound > s.bulk_message_threshold:
            findings.append(PolicyFinding(
                severity="high_risk",
                message=f"Plan sends {outbound} messages (bulk threshold {s.bulk_message_threshold}).",
            ))
        return findings

    def review_plan(
        self,
        task: str,
        plan: Plan,
        executed: list[Plan],
        intake_risk: RiskLevel = "low",
        today: str = "",
    ) -> GateResult:
        """Policy checks, then Claude's review. Human approval is a separate
        step: check needs_human() on the result, then apply_approval()."""
        findings = self.check_policy(plan, executed)
        blocks = [f for f in findings if f.severity == "block"]
        if blocks:
            # Hard policy violations are sent back without spending an LLM call.
            return GateResult(
                plan_round=plan.round,
                decision="revise",
                risk_level=intake_risk,
                concerns=[f.message for f in findings],
                feedback=" ".join(f.message for f in blocks),
                policy_findings=findings,
            )

        parts = [
            f"Today's date: {today}" if today else "",
            f"<task>\n{task}\n</task>",
            f"Risk rating at intake: {intake_risk}",
            "Plan to review:\n"
            + "\n".join(f"{s.id}. {s.tool_name}({json.dumps(s.tool_input)}): {s.description}" for s in plan.steps),
        ]
        if executed:
            parts.append(f"<already_executed>\n{format_execution_history(executed)}\n</already_executed>")
        if findings:
            parts.append("Automated policy findings:\n" + "\n".join(f"- [{f.severity}] {f.message}" for f in findings))
        assessment = self.llm.structured(GATE_SYSTEM, "\n\n".join(p for p in parts if p), PlanAssessment)

        risk = _max_risk(intake_risk, assessment.risk_level, "high" if any(f.severity == "high_risk" for f in findings) else "low")
        gate = GateResult(
            plan_round=plan.round,
            decision={"approve": "approved", "revise": "revise", "reject": "rejected"}[assessment.verdict],
            risk_level=risk,
            concerns=assessment.concerns + [f.message for f in findings if f.message not in assessment.concerns],
            feedback=assessment.feedback,
            policy_findings=findings,
        )

        return gate

    def needs_human(self, gate: GateResult) -> bool:
        """Whether an approved plan must also be signed off by a human."""
        if gate.decision != "approved":
            return False
        mode = self.settings.approval_mode
        return mode == "always" or (mode == "high_risk" and gate.risk_level == "high")

    @staticmethod
    def apply_approval(gate: GateResult, human: ApprovalDecision) -> GateResult:
        """Record a human's answer. Denied with a comment -> revise (the comment
        becomes the planner's guidance); denied without one -> rejected."""
        update: dict = {"human_approval": human}
        if not human.approved:
            update.update(decision="revise" if human.comment else "rejected", feedback=human.comment)
        return gate.model_copy(update=update)

    # --------------------------------------------------------------- report

    def report(self, task: str, plans: list[Plan], status: str, review: ReflectionResult | None = None) -> FinalReport:
        parts = [
            f"<task>\n{task}\n</task>",
            f"Run status: {status}",
            f"<execution_history>\n{format_execution_history(plans)}\n</execution_history>",
        ]
        if review is not None:
            issues = "".join(f"\n- {i}" for i in review.issues)
            parts.append(f"Reflection agent's final review ({review.status}): {review.assessment}{issues}")
        return self.llm.structured(REPORT_SYSTEM, "\n\n".join(parts), FinalReport)


def _canonical(tool_input: dict) -> str:
    return json.dumps(tool_input, sort_keys=True, default=str)


def _max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda lv: RISK_ORDER[lv])
