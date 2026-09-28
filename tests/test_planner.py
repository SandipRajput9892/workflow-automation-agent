import json

import pytest

from src.agents.base import LLMError, StructuredOutputError
from src.agents.planner_agent import PlannerAgent, PlanningError
from src.schemas import PlanDraft, PlannedStep, StepStatus

TASK = "onboard new client Rohan: send welcome email, add to CRM, schedule kickoff call"

EMAIL = {"to": "rohan@nimbus.io", "subject": "Welcome aboard!", "body": "Hi Rohan, welcome..."}
LEAD = {"name": "Rohan Mehta", "email": "rohan@nimbus.io", "company": "Nimbus Labs"}
EVENT = {"title": "Kickoff call", "attendees": ["rohan@nimbus.io"], "datetime": "2026-10-05T11:00"}


def draft(*steps, goal="Onboard Rohan"):
    """steps: (tool_name, tool_input) where tool_input is a dict or a raw JSON string."""
    return PlanDraft(
        goal=goal,
        steps=[
            PlannedStep(
                id=i, description=f"step {i}", tool_name=tool,
                tool_input_json=inp if isinstance(inp, str) else json.dumps(inp),
            )
            for i, (tool, inp) in enumerate(steps, 1)
        ],
    )


@pytest.fixture
def planner(fake_llm, registry):
    return PlannerAgent(fake_llm, registry, max_steps=5, max_attempts=3)


def test_valid_plan_first_try(planner, fake_llm):
    fake_llm.queue(draft(("send_email", EMAIL), ("create_lead", LEAD), ("create_event", EVENT)))

    plan = planner.plan(TASK, today="2026-09-28")

    assert [s.tool_name for s in plan.steps] == ["send_email", "create_lead", "create_event"]
    assert plan.steps[1].tool_input == LEAD
    assert all(s.status == StepStatus.PENDING and s.result is None for s in plan.steps)
    assert len(fake_llm.prompts) == 1
    # the planner sees the registry's tool schemas
    assert '"name": "create_lead"' in fake_llm.prompts[0] and "input_schema" in fake_llm.prompts[0]
    assert TASK in fake_llm.prompts[0]


def test_invalid_json_triggers_corrective_retry(planner, fake_llm):
    fake_llm.queue(draft(("send_email", '{"to": "rohan@nimbus.io", "subject": "Hi",'), ("create_lead", LEAD)))
    fake_llm.queue(draft(("send_email", EMAIL), ("create_lead", LEAD)))

    plan = planner.plan(TASK)

    assert plan.steps[0].tool_input == EMAIL
    corrective = fake_llm.prompts[1]
    assert corrective.startswith(fake_llm.prompts[0])  # original context preserved
    assert "not valid JSON" in corrective
    assert "<previous_attempt>" in corrective


def test_unparseable_response_triggers_retry(planner, fake_llm):
    fake_llm.queue_error(PlanDraft, StructuredOutputError("Response is not a valid PlanDraft: steps: Field required"))
    fake_llm.queue(draft(("create_lead", LEAD)))

    plan = planner.plan(TASK)

    assert len(plan.steps) == 1
    assert "steps: Field required" in fake_llm.prompts[1]
    assert "<previous_attempt>" not in fake_llm.prompts[1]


@pytest.mark.parametrize(
    "bad_step, expected",
    [
        (("create_crm_contact", LEAD), "Unknown tool 'create_crm_contact'"),
        (("create_lead", {"name": "Rohan", "email": "rohan@nimbus.io"}), "Missing required argument 'company'"),
        (("create_lead", {**LEAD, "phone": "123"}), "Unexpected argument 'phone'"),
        (("create_event", {**EVENT, "attendees": "rohan@nimbus.io"}), "'attendees' must be of type array"),
        (("update_status", {"lead_id": "LEAD-0001", "status": "onboarded"}), "must be one of"),
        (("send_email", '["not", "an", "object"]'), "must be a JSON object"),
    ],
)
def test_schema_violations_are_reported_back(planner, fake_llm, bad_step, expected):
    fake_llm.queue(draft(bad_step)).queue(draft(("create_lead", LEAD)))
    planner.plan(TASK)
    assert expected in fake_llm.prompts[1]


def test_placeholders_must_reference_earlier_steps(planner, fake_llm):
    forward = {"lead_id": "<lead_id from step 2>", "status": "contacted"}
    fake_llm.queue(draft(("update_status", forward), ("create_lead", LEAD)))
    backward = {"lead_id": "<lead_id from step 1>", "status": "contacted"}
    fake_llm.queue(draft(("create_lead", LEAD), ("update_status", backward)))

    plan = planner.plan(TASK)

    assert "refers to step 2" in fake_llm.prompts[1]
    assert plan.steps[1].tool_input["lead_id"] == "<lead_id from step 1>"


def test_structural_problems(planner, fake_llm):
    too_many = draft(*[("create_lead", LEAD)] * 6)
    fake_llm.queue(too_many).queue(PlanDraft(goal="g", steps=[])).queue(draft(("create_lead", LEAD)))
    planner.plan(TASK)
    assert "maximum is 5" in fake_llm.prompts[1]
    assert "no steps" in fake_llm.prompts[2]


def test_gives_up_after_max_attempts(planner, fake_llm):
    for _ in range(3):
        fake_llm.queue(draft(("teleport", {})))
    with pytest.raises(PlanningError) as exc_info:
        planner.plan(TASK)
    assert "after 3 attempts" in str(exc_info.value)
    assert any("teleport" in p for p in exc_info.value.problems)
    assert len(fake_llm.prompts) == 3


def test_refusal_is_not_retried(planner, fake_llm):
    fake_llm.queue_error(PlanDraft, LLMError("Claude declined the request"))
    with pytest.raises(LLMError):
        planner.plan(TASK)
    assert len(fake_llm.prompts) == 1
