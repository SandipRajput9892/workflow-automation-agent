import pytest

from src.agents.executor_agent import ExecutorAgent
from src.agents.supervisor_agent import AutoApprover, DenyApprover, SupervisorAgent
from src.schemas import (
    ApprovalDecision, Plan, PlanAssessment, ReflectionResult, RouteDecision, Step,
)

TASK = "Add Priya (priya@acme.com, Acme) as a lead and tell #sales"
LEAD = {"name": "Priya Shah", "email": "priya@acme.com", "company": "Acme"}


def make_plan(*steps, round=0):
    return Plan(goal="g", round=round,
                steps=[Step(id=i, description=f"s{i}", tool_name=t, tool_input=inp) for i, (t, inp) in enumerate(steps, 1)])


def assess(verdict="approve", risk="low", concerns=(), feedback=""):
    return PlanAssessment(verdict=verdict, risk_level=risk, concerns=list(concerns), feedback=feedback)


@pytest.fixture
def supervisor(fake_llm, registry, settings):
    return SupervisorAgent(fake_llm, registry, settings)


# ------------------------------------------------------------------ routing


def test_allowed_moves_guardrails(supervisor, settings):
    settings.max_correction_rounds = 1
    settings.max_plan_requests = 2
    one_plan = [make_plan(("send_message", {"channel": "#s", "text": "x"}))]
    revised = ReflectionResult(status="revised_plan", assessment="a", revised_plan=make_plan(round=1))
    unrecoverable = ReflectionResult(status="unrecoverable", assessment="a")
    complete = ReflectionResult(status="workflow_complete", assessment="a")

    def moves(**state):
        return supervisor.allowed_moves(state)[0]

    assert moves(event="planned") == ["executor"]
    assert moves(event="executed") == ["reflection"]
    assert moves(event="reviewed", review=complete) == ["finish"]
    assert moves(event="reviewed", review=revised, plans=one_plan, plan_requests=1) == ["executor", "planner", "finish"]
    assert moves(event="reviewed", review=unrecoverable, plans=one_plan, plan_requests=1) == ["planner", "finish"]
    # planner budget used up
    assert moves(event="reviewed", review=revised, plans=one_plan, plan_requests=2) == ["executor", "finish"]
    # correction budget used up: no more executions, so no planning either
    assert moves(event="reviewed", review=revised, plans=one_plan * 2, plan_requests=1) == ["finish"]
    assert moves(event="planning_failed", plan_requests=1) == ["planner", "finish"]
    assert moves(event="review_failed") == ["finish"]


def test_single_option_needs_no_llm(supervisor, fake_llm):
    route = supervisor.route(TASK, {"event": "executed"}, ["reflection"], "done")
    assert route.next == "reflection" and not route.by_llm
    assert fake_llm.prompts == []


def test_llm_chooses_between_options(supervisor, fake_llm):
    fake_llm.queue(RouteDecision(next="planner", reasoning="approach is wrong", guidance="use update_status on LEAD-0001"))
    state = {"event": "reviewed", "plans": [], "pending_plan": make_plan(("create_lead", LEAD), round=1),
             "review": ReflectionResult(status="revised_plan", assessment="a", issues=["lead not contacted"])}

    route = supervisor.route(TASK, state, ["executor", "planner", "finish"], "situation")

    assert route.by_llm and route.next == "planner" and route.guidance == "use update_status on LEAD-0001"
    prompt = fake_llm.prompts[0]
    assert TASK in prompt and "lead not contacted" in prompt and "Pending plan" in prompt
    assert "- executor:" in prompt and "- finish:" in prompt


def test_disallowed_choice_is_re_asked_then_falls_back_to_finish(supervisor, fake_llm, settings):
    settings.max_output_attempts = 2
    fake_llm.queue(RouteDecision(next="executor", reasoning="r", guidance=""))
    fake_llm.queue(RouteDecision(next="planner", reasoning="r", guidance=""))
    route = supervisor.route(TASK, {"event": "reviewed"}, ["planner", "finish"], "s")
    assert route.next == "planner"
    assert "not an allowed option" in fake_llm.prompts[1]

    for _ in range(2):
        fake_llm.queue(RouteDecision(next="executor", reasoning="r", guidance=""))
    assert supervisor.route(TASK, {"event": "reviewed"}, ["planner", "finish"], "s").next == "finish"


# ------------------------------------------------------------- policy checks


def test_repeat_of_successful_action_is_blocked_without_llm(supervisor, fake_llm, registry, audit):
    executed = ExecutorAgent(fake_llm, registry, audit).execute_plan(
        make_plan(("create_lead", LEAD), ("send_message", {"channel": "#sales", "text": "New lead: Priya"}))
    )
    corrective = make_plan(("send_message", {"channel": "#sales", "text": "New lead: Priya"}), round=1)

    gate = supervisor.review_plan(TASK, corrective, [executed])

    assert gate.decision == "revise"
    assert "repeats send_message" in gate.feedback
    assert fake_llm.prompts == []


def test_bulk_and_external_findings(supervisor, fake_llm, settings):
    settings.internal_domains = ["ourco.com"]
    settings.bulk_recipient_threshold = 2
    settings.bulk_message_threshold = 1
    plan = make_plan(
        ("send_email", {"to": "a@ourco.com, b@acme.com, c@acme.com", "subject": "s", "body": "b"}),
        ("send_message", {"channel": "#sales", "text": "x"}),
    )
    findings = supervisor.check_policy(plan, [])
    assert {(f.severity, f.step_id) for f in findings} == {("high_risk", 1), ("info", 1), ("high_risk", None)}
    assert any("b@acme.com, c@acme.com" in f.message for f in findings)


# ---------------------------------------------------------------- the gate


def test_gate_approves_low_risk_without_human(supervisor, fake_llm):
    fake_llm.queue(assess("approve", "low"))
    gate = supervisor.review_plan(TASK, make_plan(("create_lead", LEAD)), [], "low", "2026-09-28")
    assert gate.decision == "approved" and gate.human_approval is None
    assert '"email": "priya@acme.com"' in fake_llm.prompts[0]


@pytest.mark.parametrize("verdict, decision", [("revise", "revise"), ("reject", "rejected")])
def test_gate_revise_and_reject(supervisor, fake_llm, verdict, decision):
    fake_llm.queue(assess(verdict, "medium", ["emails the wrong person"], "email priya@acme.com instead"))
    gate = supervisor.review_plan(TASK, make_plan(("create_lead", LEAD)), [])
    assert gate.decision == decision and gate.concerns == ["emails the wrong person"]


def test_policy_high_risk_raises_llm_rating(supervisor, fake_llm, settings):
    settings.bulk_recipient_threshold = 1
    fake_llm.queue(assess("approve", "low"))
    plan = make_plan(("send_email", {"to": "a@x.com, b@x.com", "subject": "s", "body": "b"}))
    gate = supervisor.review_plan(TASK, plan, [])
    assert gate.risk_level == "high" and gate.decision == "approved"
    assert supervisor.needs_human(gate)  # default approval_mode is high_risk


@pytest.mark.parametrize("mode, risk, needed", [
    ("never", "high", False), ("high_risk", "medium", False), ("high_risk", "high", True), ("always", "low", True),
])
def test_approval_modes(supervisor, fake_llm, settings, mode, risk, needed):
    settings.approval_mode = mode
    fake_llm.queue(assess("approve", risk))
    gate = supervisor.review_plan(TASK, make_plan(("create_lead", LEAD)), [])
    assert supervisor.needs_human(gate) is needed


def test_no_human_needed_for_plans_not_approved(supervisor, fake_llm, settings):
    settings.approval_mode = "always"
    fake_llm.queue(assess("revise", "high", feedback="fix"))
    assert not supervisor.needs_human(supervisor.review_plan(TASK, make_plan(("create_lead", LEAD)), []))


@pytest.mark.parametrize("decision, expected, feedback", [
    (ApprovalDecision(approved=True), "approved", ""),
    (ApprovalDecision(approved=False), "rejected", ""),
    (ApprovalDecision(approved=False, comment="Post in #leads, not #sales"), "revise", "Post in #leads, not #sales"),
])
def test_apply_approval(supervisor, fake_llm, decision, expected, feedback):
    fake_llm.queue(assess("approve", "high"))
    gate = supervisor.review_plan(TASK, make_plan(("create_lead", LEAD)), [])
    updated = supervisor.apply_approval(gate, decision)
    assert updated.decision == expected and updated.human_approval == decision
    if expected != "approved":
        assert updated.feedback == feedback
    assert gate.human_approval is None  # original untouched


def test_builtin_approvers():
    assert AutoApprover()("t", make_plan(), None).approved
    assert not DenyApprover()("t", make_plan(), None).approved
