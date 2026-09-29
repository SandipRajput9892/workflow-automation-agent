"""Database operations for workflow runs and step logs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.db.models import StepLog, WorkflowRun, utcnow
from src import schemas as core

TERMINAL = ("completed", "failed", "rejected")
EVENT_STEP_STATUS = {"ok": "completed", "failed": "failed", "skipped": "skipped"}


# ---------------------------------------------------------------- writes


def create_run(db: Session, run_id: str, task: str) -> WorkflowRun:
    row = WorkflowRun(id=run_id, task=task, status="running")
    db.add(row)
    db.flush()
    return row


def set_status(db: Session, run_id: str, status: str, error: str | None = None) -> None:
    row = db.get(WorkflowRun, run_id)
    if row is not None:
        row.status = status
        if error is not None:
            row.error = error


def record_step_event(db: Session, run_id: str, kind: str, data: dict[str, Any]) -> None:
    """Live step logging from orchestrator step_started / step_finished events."""
    step = _get_or_new_step(db, run_id, int(data.get("round", 0)), int(data["step_id"]))
    step.tool_name = data.get("tool_name", step.tool_name or "")
    step.description = data.get("description", step.description or "")
    if kind == "step_started":
        step.status = "running"
        step.started_at = step.started_at or utcnow()
    else:
        step.status = EVENT_STEP_STATUS.get(data.get("status", ""), "failed")
        step.tool_input = data.get("tool_input")
        step.result = data.get("result")
        step.error = data.get("error")
        step.attempts = int(data.get("attempts", 0))
        step.finished_at = utcnow()


def sync_run(db: Session, run: core.WorkflowRun) -> WorkflowRun:
    """Make the DB row match the orchestrator's authoritative WorkflowRun."""
    row = db.get(WorkflowRun, run.run_id) or create_run(db, run.run_id, run.task)
    report = run.final_report
    row.status = run.status
    row.error = run.error
    row.final_report = report.model_dump(mode="json") if report else None
    row.summary = report.summary if report else None
    row.success = (run.status == "completed") if run.status in TERMINAL else None
    row.pending_approval = run.pending_approval.model_dump(mode="json") if run.pending_approval else None
    row.review = run.review.model_dump(mode="json") if run.review else None
    row.knowledge = [k.model_dump(mode="json") for k in run.knowledge]
    row.correction_rounds = run.correction_rounds
    row.needed_approval = row.needed_approval or run.status == "awaiting_approval" or any(
        g.human_approval is not None for g in run.gates
    )
    if run.status in TERMINAL:
        row.completed_at = run.finished_at or utcnow()
        row.duration_seconds = (row.completed_at - _aware(row.created_at)).total_seconds()

    # Upsert (never delete): steps of a plan still in progress exist only as live
    # event rows until that plan finishes.
    for plan in run.plans:
        for s in plan.steps:
            step = _get_or_new_step(db, run.run_id, plan.round, s.id)
            step.tool_name, step.description, step.status = s.tool_name, s.description, s.status.value
            result = s.result
            if result is not None:
                last = result.tool_calls[-1] if result.tool_calls else None
                step.tool_input = last.tool_input if last else s.tool_input
                step.result = last.result.model_dump(mode="json") if last else None
                step.summary, step.error, step.attempts = result.summary, result.error, len(result.tool_calls)
                step.finished_at = step.finished_at or result.finished_at
            else:
                step.tool_input = s.tool_input
    db.flush()
    row.step_count = db.scalar(
        select(func.count()).select_from(StepLog).where(
            StepLog.workflow_id == run.run_id, StepLog.status.in_(("completed", "failed"))
        )
    ) or 0
    return row


def mark_stale_running(db: Session) -> int:
    """Runs left 'running' by a previous server process can't still be executing."""
    rows = db.scalars(select(WorkflowRun).where(WorkflowRun.status == "running")).all()
    for row in rows:
        row.status = "interrupted"
        row.error = "Server restarted while the workflow was running."
    return len(rows)


def _get_or_new_step(db: Session, run_id: str, plan_round: int, step_id: int) -> StepLog:
    step = db.scalar(
        select(StepLog).where(StepLog.workflow_id == run_id, StepLog.plan_round == plan_round, StepLog.step_id == step_id)
    )
    if step is None:
        step = StepLog(workflow_id=run_id, plan_round=plan_round, step_id=step_id, tool_name="", status="pending")
        db.add(step)
    return step


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; values are stored in UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------- reads


def get_run(db: Session, run_id: str) -> Optional[WorkflowRun]:
    return db.scalar(select(WorkflowRun).options(selectinload(WorkflowRun.steps)).where(WorkflowRun.id == run_id))


def list_runs(
    db: Session,
    status: Optional[str] = None,
    created_from: Optional[datetime] = None,
    created_to: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WorkflowRun], int]:
    query = select(WorkflowRun)
    if status:
        query = query.where(WorkflowRun.status == status)
    if created_from:
        query = query.where(WorkflowRun.created_at >= created_from)
    if created_to:
        query = query.where(WorkflowRun.created_at < created_to)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset)).all()
    return list(rows), total


def metrics(db: Session) -> dict[str, Any]:
    by_status = dict(db.execute(select(WorkflowRun.status, func.count()).group_by(WorkflowRun.status)).all())
    total = sum(by_status.values())
    finished = sum(by_status.get(s, 0) for s in TERMINAL)
    completed = by_status.get("completed", 0)

    auto_completed = db.scalar(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.status == "completed", WorkflowRun.needed_approval.is_(False)
        )
    ) or 0
    needed_approval = db.scalar(
        select(func.count()).select_from(WorkflowRun).where(WorkflowRun.needed_approval.is_(True))
    ) or 0
    avg_steps = db.scalar(select(func.avg(WorkflowRun.step_count)).where(WorkflowRun.status.in_(TERMINAL)))
    avg_seconds = db.scalar(select(func.avg(WorkflowRun.duration_seconds)).where(WorkflowRun.status == "completed"))

    def pct(n: int, d: int) -> Optional[float]:
        return round(100.0 * n / d, 1) if d else None

    return {
        "total_workflows": total,
        "finished_workflows": finished,
        "by_status": by_status,
        "auto_completed_pct": pct(auto_completed, finished),
        "needed_approval_pct": pct(needed_approval, total),
        "avg_steps_per_workflow": round(avg_steps, 2) if avg_steps is not None else None,
        "avg_completion_time_seconds": round(avg_seconds, 2) if avg_seconds is not None else None,
        "completed_workflows": completed,
        "auto_completed_workflows": auto_completed,
        "needed_approval_workflows": needed_approval,
    }
