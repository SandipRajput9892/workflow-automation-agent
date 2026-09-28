import json

import pytest

from src.agents.executor_agent import ExecutorAgent
from src.agents.planner_agent import PlannerAgent
from src.memory.history_manager import (
    final_plan, format_past_workflows, get_similar_past_workflows, save_workflow_run,
)
from src.schemas import FinalReport, InputAdjustment, Plan, PlanDraft, PlannedStep, Step, StepStatus

ONBOARD = "Onboard new client Rohan: add to CRM and send a welcome email"
LEAD = {"name": "Rohan Mehta", "email": "rohan@nimbus.io", "company": "Nimbus Labs"}
WELCOME = {"to": "rohan@nimbus.io", "subject": "Welcome", "body": "Hi Rohan, welcome aboard! " + "x" * 300}


def plan(*steps):
    return Plan(goal="g", steps=[Step(id=i, description=d, tool_name=t, tool_input=inp)
                                 for i, (t, d, inp) in enumerate(steps, 1)])


ONBOARD_PLAN = plan(("create_lead", "Add Rohan as a CRM lead", LEAD), ("send_email", "Send welcome email", WELCOME))
DIGEST_PLAN = plan(("send_message", "Post digest", {"channel": "#sales", "text": "Weekly pipeline digest"}))


def report(success=True, summary="done"):
    return FinalReport(success=success, summary=summary, actions_taken=[], open_issues=[])


# ------------------------------------------------------------ save / retrieve


def test_save_and_get_similar(memory):
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(summary="Lead created, email sent"), run_id="r1", store=memory)
    save_workflow_run("Post the weekly pipeline digest to #sales", DIGEST_PLAN, report(), run_id="r2", store=memory)

    hits = get_similar_past_workflows("Onboard new client Maria: add her to the CRM and send a welcome email", store=memory)

    assert [h.run_id for h in hits] == ["r1"]  # the digest is beyond the relevance cutoff
    best = hits[0]
    assert best.task == ONBOARD and best.success and best.status == "completed"
    assert best.outcome == "Lead created, email sent"
    assert best.plan == ONBOARD_PLAN  # full Plan round-trips, not just text
    assert 0 <= best.distance < 0.6


def test_top_k_and_ordering(memory):
    for i, task in enumerate(["Onboard client A: add to CRM, send welcome email",
                              "Onboard client B: add to CRM and send a welcome email",
                              "Onboard client C: add to CRM"]):
        save_workflow_run(task, ONBOARD_PLAN, report(), run_id=f"r{i}", store=memory)
    hits = get_similar_past_workflows(ONBOARD, top_k=2, store=memory)
    assert len(hits) == 2
    assert hits[0].distance <= hits[1].distance


def test_only_successful_by_default(memory):
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(success=False, summary="email bounced"), run_id="bad", store=memory)
    assert get_similar_past_workflows(ONBOARD, store=memory) == []
    failed = get_similar_past_workflows(ONBOARD, successful_only=False, store=memory)
    assert [(h.run_id, h.status) for h in failed] == [("bad", "failed")]


def test_saving_same_run_id_overwrites(memory):
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(summary="first"), run_id="r1", store=memory)
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(summary="second"), run_id="r1", store=memory)
    assert memory.count() == 1
    assert get_similar_past_workflows(ONBOARD, store=memory)[0].outcome == "second"


def test_generated_run_id_and_empty_store(memory):
    assert get_similar_past_workflows(ONBOARD, store=memory) == []
    run_id = save_workflow_run(ONBOARD, ONBOARD_PLAN, report(), store=memory)
    assert run_id.startswith("run-") and memory.count() == 1
    memory.clear()
    assert memory.count() == 0


# --------------------------------------------------------------- final plan


def test_final_plan_keeps_what_actually_ran(fake_llm, registry, audit):
    executor = ExecutorAgent(fake_llm, registry, audit)
    fake_llm.queue(InputAdjustment(can_fix=False, tool_input_json="{}", rationale="no date given"))
    original = executor.execute_plan(plan(
        ("create_lead", "Add lead", LEAD),
        ("create_event", "Kickoff", {"title": "Kickoff", "attendees": ["rohan@nimbus.io"], "datetime": "soon"}),
        ("update_status", "Mark contacted", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
    ))
    assert [s.status for s in original.steps] == [StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.COMPLETED]
    correction = executor.execute_plan(
        plan(("send_message", "Announce", {"channel": "#sales", "text": "Rohan onboarded"})).model_copy(update={"round": 1})
    )

    final = final_plan([original, correction])

    # failed step dropped, correction appended, renumbered from 1
    assert [(s.id, s.tool_name) for s in final.steps] == [(1, "create_lead"), (2, "update_status"), (3, "send_message")]
    # placeholders are replaced by the values actually used
    assert final.steps[1].tool_input["lead_id"] == "LEAD-0001"


def test_final_plan_uses_retried_input(fake_llm, registry, audit):
    fake_llm.queue(InputAdjustment(
        can_fix=True, rationale="ISO",
        tool_input_json=json.dumps({"title": "Kickoff", "attendees": ["rohan@nimbus.io"], "datetime": "2026-10-05T11:00"}),
    ))
    executed = ExecutorAgent(fake_llm, registry, audit).execute_plan(
        plan(("create_event", "Kickoff", {"title": "Kickoff", "attendees": ["rohan@nimbus.io"], "datetime": "Oct 5 11am"}))
    )
    assert final_plan([executed]).steps[0].tool_input["datetime"] == "2026-10-05T11:00"


def test_format_clips_long_values(memory):
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(), run_id="r1", store=memory)
    text = format_past_workflows(get_similar_past_workflows(ONBOARD, store=memory))
    assert text.startswith("[1] Task: " + ONBOARD)
    assert "create_lead(" in text and "Outcome (completed)" in text
    assert "x" * 200 not in text


# ------------------------------------------------------------ planner usage


def _draft():
    return PlanDraft(goal="g", steps=[PlannedStep(id=1, description="d", tool_name="create_lead",
                                                  tool_input_json=json.dumps(LEAD))])


def test_planner_includes_similar_successful_workflows(fake_llm, registry, memory):
    save_workflow_run(ONBOARD, ONBOARD_PLAN, report(summary="Lead created, email sent"), run_id="r1", store=memory)
    save_workflow_run(ONBOARD + " (attempt)", ONBOARD_PLAN, report(success=False, summary="FAILED RUN"), run_id="r0", store=memory)
    fake_llm.queue(_draft())

    PlannerAgent(fake_llm, registry, memory=memory).plan("Onboard new client Maria: add her to the CRM and send a welcome email")

    prompt = fake_llm.prompts[0]
    assert "<similar_past_workflows>" in prompt and "Lead created, email sent" in prompt
    assert "FAILED RUN" not in prompt


def test_planner_skips_section_when_nothing_relevant(fake_llm, registry, memory):
    save_workflow_run("Post the weekly pipeline digest to #sales", DIGEST_PLAN, report(), run_id="r2", store=memory)
    fake_llm.queue(_draft())
    PlannerAgent(fake_llm, registry, memory=memory).plan("book a dentist appointment")
    assert "<similar_past_workflows>" not in fake_llm.prompts[0]


def test_planner_survives_memory_errors(fake_llm, registry):
    class Broken:
        def search(self, *a, **k):
            raise RuntimeError("chroma down")

    fake_llm.queue(_draft())
    assert PlannerAgent(fake_llm, registry, memory=Broken()).plan(ONBOARD).steps


# ------------------------------------------------------------ run history


def test_history_manager(history):
    history.record("run-a", "start", task="t")
    history.record("run-a", "step", data={"x": 1})
    events = history.load("run-a")
    assert [e["event"] for e in events] == ["start", "step"]
    assert history.list_runs() == ["run-a"]
