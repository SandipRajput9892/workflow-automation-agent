import json

import pytest

from src.agents.base import LLMError, StructuredOutputError, ValidationFailed
from src.agents.executor_agent import ExecutorAgent
from src.agents.reflection_agent import ReflectionAgent, format_execution_history
from src.schemas import Plan, PlannedStep, ReflectionReview, Step, StepStatus

TASK = "Onboard Rohan (rohan@nimbus.io, Nimbus Labs): add him to the CRM as contacted and email him a welcome."
LEAD = {"name": "Rohan Mehta", "email": "rohan@nimbus.io", "company": "Nimbus Labs"}
WELCOME = {"to": "rohan@nimbus.io", "subject": "Welcome!", "body": "Hi Rohan, welcome aboard."}


def make_plan(*steps, round=0):
    return Plan(
        goal="Onboard Rohan",
        round=round,
        steps=[Step(id=i, description=f"step {i}", tool_name=t, tool_input=inp) for i, (t, inp) in enumerate(steps, 1)],
    )


def review(verdict, goal_achieved, steps=(), issues=("x",)):
    return ReflectionReview(
        goal_achieved=goal_achieved,
        assessment=f"assessment: {verdict}",
        issues=list(issues),
        verdict=verdict,
        corrective_steps=[
            PlannedStep(id=i, description=f"fix {i}", tool_name=t, tool_input_json=json.dumps(inp))
            for i, (t, inp) in enumerate(steps, 1)
        ],
    )


@pytest.fixture
def executed(fake_llm, registry, audit):
    """Run a plan for real: lead is created and emailed, but never marked 'contacted'."""
    plan = make_plan(("create_lead", LEAD), ("send_email", WELCOME))
    return [ExecutorAgent(fake_llm, registry, audit).execute_plan(plan)]


@pytest.fixture
def reflector(fake_llm, registry):
    return ReflectionAgent(fake_llm, registry, max_steps=5, max_attempts=3)


def test_prompt_contains_task_and_full_history(reflector, fake_llm, executed):
    fake_llm.queue(review("workflow_complete", True, issues=()))
    reflector.review(TASK, executed)
    prompt = fake_llm.prompts[0]
    assert TASK in prompt
    assert '"lead_id": "LEAD-0001"' in prompt          # tool results
    assert '"subject": "Welcome!"' in prompt             # exact tool inputs
    assert '"name": "update_status"' in prompt           # tool schemas, for corrective steps


def test_workflow_complete(reflector, fake_llm, executed):
    fake_llm.queue(review("workflow_complete", True, issues=()))
    result = reflector.review(TASK, executed)
    assert result.status == "workflow_complete" and result.revised_plan is None


def test_missing_requirement_produces_corrective_plan(reflector, fake_llm, executed):
    # All steps succeeded ("no errors"), but the lead was never marked contacted.
    fake_llm.queue(review(
        "needs_correction", False,
        steps=[("update_status", {"lead_id": "LEAD-0001", "status": "contacted"})],
        issues=["Lead LEAD-0001 was created but never marked contacted"],
    ))

    result = reflector.review(TASK, executed)

    assert result.status == "revised_plan"
    plan = result.revised_plan
    assert plan.round == 1
    assert [(s.tool_name, s.tool_input) for s in plan.steps] == [
        ("update_status", {"lead_id": "LEAD-0001", "status": "contacted"})
    ]
    assert all(s.status == StepStatus.PENDING for s in plan.steps)
    assert result.issues == ["Lead LEAD-0001 was created but never marked contacted"]


def test_invalid_corrective_steps_are_re_requested(reflector, fake_llm, executed):
    fake_llm.queue(review("needs_correction", False, steps=[("update_status", {"lead_id": "LEAD-0001", "status": "done"})]))
    fake_llm.queue(review("needs_correction", False, steps=[("update_status", {"lead_id": "LEAD-0001", "status": "contacted"})]))

    result = reflector.review(TASK, executed)

    assert result.revised_plan.steps[0].tool_input["status"] == "contacted"
    corrective = fake_llm.prompts[1]
    assert corrective.startswith(fake_llm.prompts[0])
    assert "corrective_steps: Step 1 (update_status): 'status' must be one of" in corrective


@pytest.mark.parametrize(
    "bad, expected",
    [
        (review("workflow_complete", False), "workflow_complete but goal_achieved is false"),
        (review("needs_correction", True, steps=[("send_message", {"channel": "#x", "text": "y"})]),
         "needs_correction but goal_achieved is true"),
        (review("needs_correction", False), "corrective_steps is empty"),
    ],
)
def test_inconsistent_reviews_are_re_requested(reflector, fake_llm, executed, bad, expected):
    fake_llm.queue(bad).queue(review("workflow_complete", True, issues=()))
    assert reflector.review(TASK, executed).status == "workflow_complete"
    assert expected in fake_llm.prompts[1]


def test_no_rounds_left_forbids_correction(reflector, fake_llm, executed):
    fake_llm.queue(review("needs_correction", False, steps=[("update_status", {"lead_id": "LEAD-0001", "status": "contacted"})]))
    fake_llm.queue(review("unrecoverable", False))
    result = reflector.review(TASK, executed, corrections_left=0)
    assert result.status == "unrecoverable"
    assert "Correction rounds remaining: 0" in fake_llm.prompts[0]
    assert "use unrecoverable" in fake_llm.prompts[1]


def test_unparseable_then_valid(reflector, fake_llm, executed):
    fake_llm.queue_error(ReflectionReview, StructuredOutputError("Response is not a valid ReflectionReview"))
    fake_llm.queue(review("unrecoverable", False))
    assert reflector.review(TASK, executed).status == "unrecoverable"


def test_gives_up_and_refusals_propagate(reflector, fake_llm, executed):
    for _ in range(3):
        fake_llm.queue(review("needs_correction", False))
    with pytest.raises(ValidationFailed):
        reflector.review(TASK, executed)

    fake_llm.queue_error(ReflectionReview, LLMError("declined"))
    with pytest.raises(LLMError):
        reflector.review(TASK, executed)


def test_corrective_round_numbering(reflector, fake_llm, executed):
    round1 = make_plan(("update_status", {"lead_id": "LEAD-0001", "status": "hot"}), round=1)
    fake_llm.queue(review("needs_correction", False, steps=[("update_status", {"lead_id": "LEAD-0001", "status": "contacted"})]))
    result = reflector.review(TASK, [*executed, round1])
    assert result.revised_plan.round == 2


def test_history_format_covers_failures_and_skips(fake_llm, registry, audit):
    from src.schemas import InputAdjustment

    plan = make_plan(
        ("create_lead", {**LEAD, "email": "bad"}),
        ("update_status", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
    )
    fake_llm.queue(InputAdjustment(can_fix=False, tool_input_json="{}", rationale="no valid email"))
    executed = ExecutorAgent(fake_llm, registry, audit).execute_plan(plan)

    text = format_execution_history([executed])
    assert "Step 1 [failed] create_lead" in text and "Invalid email address" in text
    assert "Step 2 [skipped] update_status" in text and "depends on step 1" in text
    assert text.startswith("== Original plan ==")
