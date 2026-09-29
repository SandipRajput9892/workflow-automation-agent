"""API tests: real orchestrator + scripted fake LLM, SQLite in a temp dir."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from src.orchestrator import WorkflowOrchestrator
from src.schemas import (
    FinalReport, PlanAssessment, PlanDraft, PlannedStep, ReflectionReview, SupervisorDecision,
)

LEAD = {"name": "Priya Shah", "email": "priya@acme.com", "company": "Acme"}
ANNOUNCE = {"channel": "#sales", "text": "New lead: Priya"}


def decision(risk="medium", approved=True):
    return SupervisorDecision(approved=approved, risk_level=risk, normalized_task="Add Priya and announce",
                              required_tools=[], reasoning="ok")


def plan(*steps):
    return PlanDraft(goal="g", steps=[PlannedStep(id=i, description=d, tool_name=t, tool_input_json=json.dumps(inp))
                                      for i, (d, t, inp) in enumerate(steps, 1)])


def approve(risk="low"):
    return PlanAssessment(verdict="approve", risk_level=risk, concerns=[], feedback="")


def complete():
    return ReflectionReview(goal_achieved=True, assessment="done", issues=[], verdict="workflow_complete", corrective_steps=[])


def report(success=True):
    return FinalReport(success=success, summary="All done" if success else "Failed", actions_taken=["x"], open_issues=[])


def queue_happy(fake_llm, risk="medium", email="priya@acme.com"):
    fake_llm.queue(decision(risk)).queue(plan(
        ("Add lead", "create_lead", {**LEAD, "email": email}),
        ("Mark contacted", "update_status", {"lead_id": "<lead_id from step 1>", "status": "contacted"}),
        ("Announce", "send_message", ANNOUNCE),
    )).queue(approve())


@pytest.fixture
def client(settings, fake_llm, registry, memory, history, audit, tmp_path):
    settings.backend_database_url = f"sqlite:///{(tmp_path / 'api.sqlite').as_posix()}"

    def factory(s):
        return WorkflowOrchestrator(s, llm=fake_llm, registry=registry, memory=memory, history=history,
                                    audit=audit, approver=None, today="2026-09-28")

    with TestClient(create_app(settings, factory)) as c:
        yield c


def test_create_workflow_waits_for_result(client, fake_llm):
    queue_happy(fake_llm)
    fake_llm.queue(complete()).queue(report())

    r = client.post("/workflows", json={"task": "Add Priya from Acme and tell #sales"})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["websocket_url"] == f"/ws/workflows/{body['workflow_id']}"
    result = body["result"]
    assert result["success"] is True and result["summary"] == "All done"
    assert result["step_count"] == 3 and result["needed_approval"] is False
    assert [(s["step_id"], s["tool_name"], s["status"]) for s in result["steps"]] == [
        (1, "create_lead", "completed"), (2, "update_status", "completed"), (3, "send_message", "completed")]
    assert result["steps"][1]["tool_input"]["lead_id"] == "LEAD-0001"  # placeholder resolved
    assert result["steps"][0]["result"]["data"]["lead_id"] == "LEAD-0001"
    assert result["duration_seconds"] >= 0


def test_get_workflow_and_404(client, fake_llm):
    queue_happy(fake_llm)
    fake_llm.queue(complete()).queue(report())
    wid = client.post("/workflows", json={"task": "t"}).json()["workflow_id"]

    detail = client.get(f"/workflows/{wid}").json()
    assert detail["workflow_id"] == wid and len(detail["steps"]) == 3
    assert detail["final_report"]["summary"] == "All done"
    assert detail["steps"][0]["started_at"] and detail["steps"][0]["finished_at"]
    assert client.get("/workflows/nope").status_code == 404


def test_validation(client):
    assert client.post("/workflows", json={"task": ""}).status_code == 422
    assert client.post("/workflows", json={}).status_code == 422


def test_approval_flow(client, fake_llm, audit):
    queue_happy(fake_llm, risk="high")  # high risk -> human approval required -> pauses

    paused = client.post("/workflows", json={"task": "t"}).json()
    wid = paused["workflow_id"]
    assert paused["status"] == "awaiting_approval"
    pending = paused["result"]["pending_approval"]
    assert [s["tool_name"] for s in pending["plan"]["steps"]] == ["create_lead", "update_status", "send_message"]
    assert pending["gate"]["risk_level"] == "high"
    assert paused["result"]["steps"] == [] and audit.load() == []

    fake_llm.queue(complete()).queue(report())
    done = client.post(f"/workflows/{wid}/approve", json={"approver": "alice"}).json()

    assert done["status"] == "completed"
    assert done["result"]["needed_approval"] is True and done["result"]["pending_approval"] is None
    assert len(done["result"]["steps"]) == 3

    # nothing left to approve
    assert client.post(f"/workflows/{wid}/approve").status_code == 409
    assert client.post("/workflows/nope/approve").status_code == 404


def test_decline_without_comment_rejects(client, fake_llm):
    queue_happy(fake_llm, risk="high")
    wid = client.post("/workflows", json={"task": "t"}).json()["workflow_id"]
    r = client.post(f"/workflows/{wid}/approve", json={"approved": False})
    assert r.json()["status"] == "rejected"


def test_list_with_filters(client, fake_llm):
    queue_happy(fake_llm)
    fake_llm.queue(complete()).queue(report())
    done = client.post("/workflows", json={"task": "first"}).json()["workflow_id"]
    queue_happy(fake_llm, risk="high")
    paused = client.post("/workflows", json={"task": "second"}).json()["workflow_id"]

    all_runs = client.get("/workflows").json()
    assert all_runs["total"] == 2 and [w["workflow_id"] for w in all_runs["items"]] == [paused, done]  # newest first

    assert [w["workflow_id"] for w in client.get("/workflows", params={"status": "completed"}).json()["items"]] == [done]
    assert client.get("/workflows", params={"status": "awaiting_approval"}).json()["total"] == 1
    assert client.get("/workflows", params={"status": "bogus"}).status_code == 422

    today = datetime.now(timezone.utc).date()  # the API filters on UTC dates
    assert client.get("/workflows", params={"from": str(today), "to": str(today)}).json()["total"] == 2
    assert client.get("/workflows", params={"from": str(today + timedelta(days=1))}).json()["total"] == 0
    assert client.get("/workflows", params={"limit": 1}).json()["items"][0]["workflow_id"] == paused


def test_metrics(client, fake_llm):
    empty = client.get("/metrics").json()
    assert empty["total_workflows"] == 0 and empty["auto_completed_pct"] is None

    queue_happy(fake_llm)                        # 1: completes automatically (3 steps)
    fake_llm.queue(complete()).queue(report())
    client.post("/workflows", json={"task": "a"})

    queue_happy(fake_llm, "high", "b@acme.com")  # 2: needs approval, then completes (3 steps)
    wid = client.post("/workflows", json={"task": "b"}).json()["workflow_id"]
    fake_llm.queue(complete()).queue(report())
    client.post(f"/workflows/{wid}/approve")

    queue_happy(fake_llm, "high", "c@acme.com")  # 3: needs approval, still pending
    client.post("/workflows", json={"task": "c"})

    m = client.get("/metrics").json()
    assert m["total_workflows"] == 3 and m["finished_workflows"] == 2
    assert m["auto_completed_pct"] == 50.0      # 1 of 2 finished completed with no human
    assert m["needed_approval_pct"] == 66.7     # 2 of 3
    assert m["avg_steps_per_workflow"] == 3.0
    assert m["avg_completion_time_seconds"] >= 0
    assert m["by_status"] == {"completed": 2, "awaiting_approval": 1}
    assert m["auto_completed_workflows"] == 1 and m["needed_approval_workflows"] == 2


def test_websocket_streams_live_updates(client, fake_llm):
    queue_happy(fake_llm)
    fake_llm.queue(complete()).queue(report())

    started = client.post("/workflows", params={"wait": "false"}, json={"task": "t"})
    assert started.status_code == 202
    wid = started.json()["workflow_id"]

    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        messages = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["kind"] == "run_completed":
                break

    kinds = [m["kind"] for m in messages]
    assert kinds[0] == "snapshot"
    # replayed + live events cover the whole run, in order, without duplicates
    assert kinds[1:] == ["run_started", "intake", "knowledge", "plan_created", "gate",
                         "step_started", "step_finished", "step_started", "step_finished",
                         "step_started", "step_finished", "reflection", "run_completed"]
    finished = [m for m in messages if m["kind"] == "step_finished"]
    assert finished[0]["data"]["result"]["data"]["lead_id"] == "LEAD-0001"
    assert messages[-1]["workflow"]["status"] == "completed"
    assert all(m["workflow_id"] == wid for m in messages)


def test_websocket_for_finished_and_unknown_workflows(client, fake_llm):
    from starlette.websockets import WebSocketDisconnect

    queue_happy(fake_llm)
    fake_llm.queue(complete()).queue(report())
    wid = client.post("/workflows", json={"task": "t"}).json()["workflow_id"]
    with client.websocket_connect(f"/ws/workflows/{wid}") as ws:
        first = ws.receive_json()
        assert first["kind"] == "snapshot" and first["workflow"]["status"] == "completed"

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/workflows/nope") as ws:
            ws.receive_json()
    assert exc.value.code == 4404


def test_cors_headers(client):
    r = client.options("/workflows", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] in ("*", "http://localhost:5173")


def test_stale_running_rows_marked_interrupted(settings, tmp_path, fake_llm, registry, memory, history, audit):
    from backend.db import crud
    from backend.db.database import Database

    url = f"sqlite:///{(tmp_path / 'stale.sqlite').as_posix()}"
    db = Database(url)
    db.create_all()
    with db.session() as s:
        crud.create_run(s, "old-run", "t")
    settings.backend_database_url = url
    app = create_app(settings, lambda s: WorkflowOrchestrator(s, llm=fake_llm, registry=registry, memory=memory,
                                                              history=history, audit=audit, today="2026-09-28"))
    with TestClient(app) as c:
        detail = c.get("/workflows/old-run").json()
    assert detail["status"] == "interrupted" and "restarted" in detail["error"]
