import json

import pytest

from src.agents.supervisor_agent import AutoApprover
from src.orchestrator import WorkflowOrchestrator
from src.schemas import (
    ApprovalDecision, FinalReport, InputAdjustment, PlanAssessment, PlanDraft, PlannedStep, ReflectionReview,
    RouteDecision, StepStatus, SupervisorDecision, WorkflowRun,
)

LEAD = {"name": "Priya Shah", "email": "priya@acme.com", "company": "Acme"}
ANNOUNCE = {"channel": "#sales", "text": "New lead: Priya"}


def _decision(approved=True, risk="medium"):
    return SupervisorDecision(
        approved=approved, risk_level=risk, normalized_task="Add Priya as a contacted lead and announce it",
        required_tools=["create_lead", "update_status", "send_message"], reasoning="looks fine",
    )


def _steps(*steps):
    return [PlannedStep(id=i, description=d, tool_name=t, tool_input_json=json.dumps(inp))
            for i, (d, t, inp) in enumerate(steps, 1)]


def _plan(*steps):
    return PlanDraft(goal="g", steps=_steps(*steps))


def _approve(risk="low"):
    return PlanAssessment(verdict="approve", risk_level=risk, concerns=[], feedback="")


def _review(verdict, achieved, *steps):
    return ReflectionReview(
        goal_achieved=achieved, assessment=verdict, issues=[] if achieved else ["issue"], verdict=verdict,
        corrective_steps=_steps(*steps),
    )


def _route(nxt, guidance=""):
    return RouteDecision(next=nxt, reasoning=f"go {nxt}", guidance=guidance)


def _report(success=True):
    return FinalReport(success=success, summary="summary", actions_taken=["a"], open_issues=[])


@pytest.fixture
def orch(settings, fake_llm, registry, memory, history, audit):
    def make(approver=None):
        return WorkflowOrchestrator(
            settings, llm=fake_llm, registry=registry, memory=memory, history=history, audit=audit,
            approver=approver, today="2026-09-28",
        )
    return make


def test_happy_path(orch, fake_llm, data_dir, memory, history, audit):
    fake_llm.queue(_decision()).queue(_plan(
        ("Add lead", "create_lead", LEAD),
        ("Mark contacted", "update_status", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
    ))
    fake_llm.queue(_approve()).queue(_review("workflow_complete", True)).queue(_report())

    run = orch().run("Add Priya")

    assert isinstance(run, WorkflowRun) and run.status == "completed"
    assert json.loads((data_dir / "crm.json").read_text())["leads"][0]["status"] == "contacted"
    assert [(r.after, r.next, r.by_llm) for r in run.routes] == [
        ("start", "planner", False), ("planned", "executor", False),
        ("executed", "reflection", False), ("reviewed", "finish", False),
    ]
    assert [g.decision for g in run.gates] == ["approved"]
    assert memory.count() == 1
    events = [e["event"] for e in history.load(run.run_id)]
    assert events[:3] == ["start", "supervisor_review", "route"] and "gate" in events
    assert {e["run_id"] for e in audit.load()} == {run.run_id}


def test_rejected_at_intake(orch, fake_llm, memory, audit):
    fake_llm.queue(_decision(approved=False))
    run = orch().run("spam everyone")
    assert run.status == "rejected" and run.plans == [] and run.gates == []
    assert run.final_report.summary.startswith("Task rejected by supervisor")
    assert memory.count() == 0 and audit.load() == []


def test_supervisor_chooses_to_execute_corrective_plan(orch, fake_llm, data_dir, audit):
    fake_llm.queue(_decision()).queue(_plan(("Add lead", "create_lead", LEAD))).queue(_approve())
    fake_llm.queue(_review("needs_correction", False,
                           ("Mark contacted", "update_status", {"lead_id": "LEAD-0001", "status": "contacted"})))
    fake_llm.queue(_route("executor")).queue(_approve())  # routing choice, then gate on the corrective plan
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())

    run = orch().run("Add Priya as contacted")

    assert run.status == "completed" and [p.round for p in run.plans] == [0, 1]
    assert [g.plan_round for g in run.gates] == [0, 1]
    assert any(r.by_llm and r.next == "executor" for r in run.routes)
    assert json.loads((data_dir / "crm.json").read_text())["leads"][0]["status"] == "contacted"


def test_supervisor_chooses_to_replan_with_guidance(orch, fake_llm):
    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", {"channel": "#general", "text": "hi"})))
    fake_llm.queue(_approve()).queue(_review("unrecoverable", False))
    fake_llm.queue(_route("planner", guidance="The team channel is #sales"))
    fake_llm.queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())

    run = orch().run("tell the team")

    assert run.status == "completed" and [p.round for p in run.plans] == [0, 1]
    replan_prompt = fake_llm.prompts[5]
    assert "The team channel is #sales" in replan_prompt and "<work_so_far>" in replan_prompt


def test_gate_blocks_repeat_and_sends_back_to_planner(orch, fake_llm):
    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    # reflection wrongly proposes re-sending the same message
    fake_llm.queue(_review("needs_correction", False, ("Announce again", "send_message", ANNOUNCE)))
    fake_llm.queue(_route("executor"))
    # gate blocks it without an LLM call -> planner
    fake_llm.queue(_plan(("Log it", "send_message", {"channel": "#sales", "text": "Lead announced"}))).queue(_approve())
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())

    run = orch().run("announce")

    assert [g.decision for g in run.gates] == ["approved", "revise", "approved"]
    assert "repeats send_message" in run.gates[1].feedback
    assert run.status == "completed"
    assert [p.round for p in run.plans] == [0, 1]  # the blocked plan never executed


def test_gate_rejection_ends_run(orch, fake_llm, audit):
    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", ANNOUNCE)))
    fake_llm.queue(PlanAssessment(verdict="reject", risk_level="high", concerns=["contacts an unrelated channel"], feedback=""))
    run = orch().run("t")
    assert run.status == "rejected" and run.plans == [] and audit.load() == []
    assert "contacts an unrelated channel" in run.final_report.summary


def test_high_risk_plan_pauses_then_resumes_after_approval(orch, fake_llm, audit, history):
    fake_llm.queue(_decision(risk="high")).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve("low"))

    paused = orch().run("t")  # no approver -> pause

    assert paused.status == "awaiting_approval" and paused.final_report is None
    assert paused.pending_approval.plan.steps[0].tool_name == "send_message"
    assert paused.pending_approval.gate.risk_level == "high"
    assert audit.load() == []  # nothing executed
    assert [r.run_id for r in orch().pending_approvals()] == [paused.run_id]

    # A fresh orchestrator (as in a new process) resumes from the checkpoint.
    # Only reflection + report are queued: the gate's LLM review must NOT run again.
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())
    run = orch().resume(paused.run_id, ApprovalDecision(approved=True, approver="cli"))

    assert run.status == "completed" and run.pending_approval is None
    assert run.gates[0].human_approval.approver == "cli"
    assert [e["tool"] for e in audit.load()] == ["send_message"]
    assert orch().pending_approvals() == []
    assert "resumed" in [e["event"] for e in history.load(run.run_id)]


def test_rejecting_paused_run_with_comment_replans(orch, fake_llm):
    fake_llm.queue(_decision(risk="high")).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    paused = orch().run("t")

    fake_llm.queue(_plan(("Announce", "send_message", {"channel": "#leads", "text": "New lead"}))).queue(_approve())
    again = orch().resume(paused.run_id, ApprovalDecision(approved=False, comment="use #leads"))

    assert again.status == "awaiting_approval"  # the revised plan is still high risk
    assert again.pending_approval.plan.steps[0].tool_input["channel"] == "#leads"
    assert "use #leads" in fake_llm.prompts[-2]  # reached the planner

    fake_llm.queue(_review("workflow_complete", True)).queue(_report())
    assert orch().resume(paused.run_id, ApprovalDecision(approved=True)).status == "completed"


def test_rejecting_paused_run_without_comment_cancels(orch, fake_llm, audit):
    fake_llm.queue(_decision(risk="high")).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    paused = orch().run("t")
    run = orch().resume(paused.run_id, ApprovalDecision(approved=False))
    assert run.status == "rejected" and audit.load() == []
    assert run.final_report.summary.startswith("Plan rejected")


def test_resume_errors(orch, fake_llm):
    with pytest.raises(KeyError):
        orch().resume("no-such-run")
    fake_llm.queue(_decision(risk="high")).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    paused = orch().run("t")
    with pytest.raises(ValueError, match="waiting for approval"):
        orch().resume(paused.run_id)


def test_crashed_run_resumes_without_redoing_steps(orch, fake_llm, audit, data_dir):
    fake_llm.queue(_decision()).queue(_plan(
        ("Add lead", "create_lead", LEAD),
        ("Announce", "send_message", {"channel": "#Bad Channel", "text": "New lead"}),
    )).queue(_approve())
    # Step 2 fails and the executor asks the LLM for a fix - but nothing is queued,
    # so the fake LLM raises: an unexpected crash in the middle of the plan.
    crashed = orch().run("t")

    assert crashed.status == "interrupted" and "IndexError" in crashed.error
    assert [e["tool"] for e in audit.load() if e["action"] == "tool_call"] == ["create_lead", "send_message"]

    fake_llm.queue(InputAdjustment(can_fix=True, tool_input_json=json.dumps(ANNOUNCE), rationale="valid channel"))
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())
    run = orch().resume(crashed.run_id)

    assert run.status == "completed"
    calls = [e["tool"] for e in audit.load() if e["action"] == "tool_call"]
    assert calls.count("create_lead") == 1  # step 1 was checkpointed, not executed again
    assert len(json.loads((data_dir / "crm.json").read_text())["leads"]) == 1


def test_progress_events_stream(orch, fake_llm):
    fake_llm.queue(_decision()).queue(_plan(
        ("Add lead", "create_lead", LEAD), ("Announce", "send_message", ANNOUNCE),
    )).queue(_approve()).queue(_review("workflow_complete", True)).queue(_report())

    events = list(orch().stream("Add Priya"))

    kinds = [e.kind for e in events]
    assert kinds == [
        "run_started", "intake", "knowledge", "plan_created", "gate",
        "step_started", "step_finished", "step_started", "step_finished",
        "reflection", "run_completed",
    ]
    assert events[6].data["status"] == "ok" and events[6].data["result"]["data"]["lead_id"] == "LEAD-0001"
    assert events[6].data["tool_input"] == LEAD and events[6].data["attempts"] == 1
    assert events[-1].run.status == "completed"
    assert {e.run_id for e in events} == {events[0].run_id}


def test_on_event_callback(orch, fake_llm):
    fake_llm.queue(_decision(approved=False))
    seen = []
    run = orch().run("spam", on_event=seen.append)
    assert [e.kind for e in seen] == ["run_started", "intake", "run_rejected"]
    assert seen[-1].run == run


def test_human_approves_high_risk_plan(orch, fake_llm):
    fake_llm.queue(_decision(risk="high")).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())
    run = orch(AutoApprover()).run("t")
    assert run.status == "completed" and run.gates[0].human_approval.approved


def test_human_feedback_triggers_replan(orch, fake_llm, settings):
    settings.approval_mode = "always"

    class Feedback:
        calls = 0

        def __call__(self, task, plan, gate):
            self.calls += 1
            return ApprovalDecision(approved=self.calls > 1, comment="" if self.calls > 1 else "use #leads")

    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    fake_llm.queue(_plan(("Announce", "send_message", {"channel": "#leads", "text": "New lead"}))).queue(_approve())
    fake_llm.queue(_review("workflow_complete", True)).queue(_report())

    run = orch(Feedback()).run("t")

    assert run.status == "completed"
    assert run.plans[0].steps[0].tool_input["channel"] == "#leads"
    assert "use #leads" in fake_llm.prompts[3]


def test_budgets_stop_the_loop(orch, fake_llm, settings):
    settings.max_correction_rounds = 1
    fix = ("Retry", "update_status", {"lead_id": "LEAD-0404", "status": "won"})
    cannot = InputAdjustment(can_fix=False, tool_input_json="{}", rationale="no such lead")
    fake_llm.queue(_decision()).queue(_plan(fix)).queue(_approve()).queue(cannot)
    fake_llm.queue(_review("needs_correction", False, ("Retry", "update_status", {"lead_id": "LEAD-0405", "status": "won"})))
    fake_llm.queue(_route("executor")).queue(_approve()).queue(cannot)
    fake_llm.queue(_review("unrecoverable", False))  # no rounds left -> only "finish" allowed, no routing call
    fake_llm.queue(_report(success=False))

    run = orch().run("doomed")

    assert run.status == "failed" and len(run.plans) == 2
    assert run.routes[-1].options == ["finish"] and not run.routes[-1].by_llm


def test_planning_failure_can_be_retried_then_finishes(orch, fake_llm, settings):
    settings.max_output_attempts = 1
    settings.max_plan_requests = 2
    fake_llm.queue(_decision()).queue(_plan(("x", "teleport", {})))
    fake_llm.queue(_route("planner", guidance="only use the listed tools")).queue(_plan(("x", "teleport", {})))
    fake_llm.queue(_report(success=False))  # second failure: planner budget spent -> finish without routing call

    run = orch().run("t")

    assert run.status == "failed" and run.plans == []
    assert [r.next for r in run.routes] == ["planner", "planner", "finish"]


def test_reflection_failure_ends_run(orch, fake_llm, settings, history):
    settings.max_output_attempts = 1
    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", ANNOUNCE))).queue(_approve())
    fake_llm.queue(_review("needs_correction", False))  # invalid: no steps
    fake_llm.queue(_report(success=False))
    run = orch().run("t")
    assert run.status == "failed"
    assert "reflection_failed" in [e["event"] for e in history.load(run.run_id)]


def test_second_run_learns_from_first(orch, fake_llm):
    fake_llm.queue(_decision()).queue(_plan(("Add lead", "create_lead", LEAD), ("Announce", "send_message", ANNOUNCE)))
    fake_llm.queue(_approve()).queue(_review("workflow_complete", True)).queue(_report())
    first = orch().run("Add Priya from Acme as a lead and announce it in #sales")
    assert first.status == "completed"

    fake_llm.prompts.clear()
    fake_llm.queue(_decision()).queue(_plan(("Announce", "send_message", {"channel": "#sales", "text": "New lead: Tom"})))
    fake_llm.queue(_approve()).queue(_review("workflow_complete", True)).queue(_report())
    orch().run("Add Tom from Globex as a lead and announce it in #sales")

    planner_prompt = fake_llm.prompts[1]
    assert "<similar_past_workflows>" in planner_prompt
    assert '"email": "priya@acme.com"' in planner_prompt  # the plan that actually ran last time
