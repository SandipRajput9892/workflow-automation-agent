import json

import pytest

from src.agents.base import LLMError
from src.agents.executor_agent import ExecutorAgent
from src.schemas import InputAdjustment, Plan, Step, StepStatus

LEAD = {"name": "Rohan Mehta", "email": "rohan@nimbus.io", "company": "Nimbus Labs"}
WELCOME = {"to": "rohan@nimbus.io", "subject": "Welcome!", "body": "Hi Rohan, welcome to the team."}
KICKOFF = {"title": "Kickoff call", "attendees": ["rohan@nimbus.io"], "datetime": "2026-10-05T11:00"}


def make_plan(*steps):
    return Plan(
        goal="Onboard Rohan",
        steps=[Step(id=i, description=f"step {i}", tool_name=t, tool_input=inp) for i, (t, inp) in enumerate(steps, 1)],
    )


def fix(tool_input, rationale="fixed"):
    return InputAdjustment(can_fix=True, tool_input_json=json.dumps(tool_input), rationale=rationale)


def cannot(rationale):
    return InputAdjustment(can_fix=False, tool_input_json="{}", rationale=rationale)


@pytest.fixture
def executor(fake_llm, registry, audit):
    return ExecutorAgent(fake_llm, registry, audit, max_retries=1)


def test_executes_all_steps_in_order(executor, fake_llm, audit, data_dir):
    plan = make_plan(
        ("send_email", WELCOME),
        ("create_lead", LEAD),
        ("update_status", {"lead_id": "<lead_id from step 2>", "status": "contacted"}),
        ("create_event", KICKOFF),
        ("send_message", {"channel": "#sales", "text": "Onboarded Rohan (<lead_id from step 2>)"}),
    )

    result = executor.execute_plan(plan, "onboard Rohan", run_id="run-1")

    assert [s.status for s in result.steps] == [StepStatus.COMPLETED] * 5
    assert all(s.result.success and len(s.result.tool_calls) == 1 for s in result.steps)
    # placeholders resolved from step 2's result - as a whole value and inside text
    assert result.steps[2].result.tool_calls[0].tool_input["lead_id"] == "LEAD-0001"
    assert result.steps[4].result.tool_calls[0].tool_input["text"] == "Onboarded Rohan (LEAD-0001)"
    assert fake_llm.prompts == []  # happy path needs no LLM calls
    # original plan untouched
    assert all(s.status == StepStatus.PENDING for s in plan.steps)
    # side effects landed in the mock APIs
    assert json.loads((data_dir / "crm.json").read_text())["leads"][0]["status"] == "contacted"

    log = audit.load()
    assert [e["action"] for e in log] == ["tool_call"] * 5
    first = log[0]
    assert set(first) >= {"timestamp", "step", "step_id", "tool", "input", "result"}
    assert first["tool"] == "send_email" and first["input"] == WELCOME
    assert first["result"]["success"] is True and first["run_id"] == "run-1"


def test_failed_step_retries_once_with_adjusted_input(executor, fake_llm, audit):
    plan = make_plan(("create_event", {**KICKOFF, "datetime": "Oct 5th at 11am"}))
    fake_llm.queue(fix(KICKOFF, "use ISO datetime"))

    step = executor.execute_plan(plan).steps[0]

    assert step.status == StepStatus.COMPLETED
    calls = step.result.tool_calls
    assert [c.result.success for c in calls] == [False, True]
    assert calls[1].tool_input["datetime"] == "2026-10-05T11:00"
    assert "after 1 retry" in step.result.summary
    # Claude saw the error and the tool schema
    assert "Invalid datetime" in fake_llm.prompts[0] and '"name": "create_event"' in fake_llm.prompts[0]
    log = audit.load()
    assert [(e["action"], e["attempt"]) for e in log] == [("tool_call", 1), ("input_adjusted", 2), ("tool_call", 2)]
    assert {e["plan_round"] for e in log} == {0}


def test_marked_failed_when_retry_also_fails(executor, fake_llm):
    plan = make_plan(("send_message", {"channel": "#Sales Team", "text": "hi"}))
    fake_llm.queue(fix({"channel": "#sales"}))  # missing "text" -> fails schema check, never sent to the tool

    step = executor.execute_plan(plan).steps[0]

    assert step.status == StepStatus.FAILED
    assert len(step.result.tool_calls) == 1  # invalid adjustment is not sent to the tool
    assert "adjusted input is invalid" in step.result.error


def test_retry_that_fails_at_the_tool(executor, fake_llm):
    plan = make_plan(("update_status", {"lead_id": "LEAD-0999", "status": "won"}))
    fake_llm.queue(fix({"lead_id": "LEAD-0998", "status": "won"}))  # still no such lead

    step = executor.execute_plan(plan).steps[0]

    assert step.status == StepStatus.FAILED
    assert [c.result.success for c in step.result.tool_calls] == [False, False]
    assert "No lead" in step.result.error


def test_existing_lead_is_reused_by_later_steps(executor, fake_llm, registry, data_dir):
    registry.execute("create_lead", LEAD)  # LEAD-0001 already exists
    plan = make_plan(
        ("create_lead", LEAD),
        ("update_status", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
    )

    first, second = executor.execute_plan(plan).steps

    assert first.status == second.status == StepStatus.COMPLETED
    assert first.result.output()["created"] is False
    assert second.result.tool_calls[0].tool_input["lead_id"] == "LEAD-0001"
    assert fake_llm.prompts == []  # no repair needed
    leads = json.loads((data_dir / "crm.json").read_text())["leads"]
    assert [(l["id"], l["status"]) for l in leads] == [("LEAD-0001", "contacted")]


def test_no_retry_when_claude_says_unfixable(executor, fake_llm, audit):
    plan = make_plan(("update_status", {"lead_id": "LEAD-0999", "status": "won"}))
    fake_llm.queue(cannot("No such lead exists and none was created in this plan"))

    step = executor.execute_plan(plan).steps[0]

    assert step.status == StepStatus.FAILED
    assert len(step.result.tool_calls) == 1
    assert "not retried: No such lead" in step.result.error
    adjust = [e for e in audit.load() if e["action"] == "input_adjusted"][0]
    assert adjust["result"]["retrying"] is False


def test_llm_error_during_adjustment_marks_failed(executor, fake_llm):
    plan = make_plan(("send_email", {**WELCOME, "to": "rohan"}))
    fake_llm.queue_error(InputAdjustment, LLMError("declined"))
    step = executor.execute_plan(plan).steps[0]
    assert step.status == StepStatus.FAILED and "declined" in step.result.error


def test_dependent_steps_skipped_but_independent_steps_run(executor, fake_llm, audit):
    plan = make_plan(
        ("create_lead", {**LEAD, "email": "not-an-email"}),
        ("update_status", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
        ("send_email", WELCOME),
    )
    fake_llm.queue(cannot("the task gives no valid email for Rohan"))

    result = executor.execute_plan(plan)

    assert [s.status for s in result.steps] == [StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.COMPLETED]
    assert result.steps[1].result.skipped and result.steps[1].result.tool_calls == []
    assert "depends on step 1" in result.steps[1].result.error
    assert "step_skipped" in [e["action"] for e in audit.load()]


def test_bad_placeholder_field_is_repaired(executor, fake_llm):
    plan = make_plan(
        ("create_lead", LEAD),
        ("update_status", {"lead_id": "<id from step 1>", "status": "qualified"}),
    )
    fake_llm.queue(fix({"lead_id": "<lead_id from step 1>", "status": "qualified"}))

    step = executor.execute_plan(plan).steps[1]

    assert step.status == StepStatus.COMPLETED
    assert step.result.tool_calls[0].tool_input["lead_id"] == "LEAD-0001"
    assert "has no field 'id'" in fake_llm.prompts[0]
    assert "LEAD-0001" in fake_llm.prompts[0]  # earlier results shown to Claude


def test_max_retries_zero(fake_llm, registry, audit):
    executor = ExecutorAgent(fake_llm, registry, audit, max_retries=0)
    step = executor.execute_plan(make_plan(("send_email", {**WELCOME, "to": "rohan"}))).steps[0]
    assert step.status == StepStatus.FAILED and fake_llm.prompts == []


def test_resumes_partially_executed_plan(executor, registry):
    plan = executor.execute_plan(make_plan(("create_lead", LEAD)))
    plan.steps.append(Step(id=2, description="s2", tool_name="update_status",
                           tool_input={"lead_id": "<lead_id from step 1>", "status": "won"}))
    result = executor.execute_plan(plan)
    assert [s.status for s in result.steps] == [StepStatus.COMPLETED, StepStatus.COMPLETED]
    assert len(result.steps[0].result.tool_calls) == 1  # step 1 was not re-run
