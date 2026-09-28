"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from backend.db import models


class WorkflowCreate(BaseModel):
    task: str = Field(min_length=1, max_length=5000, description="Natural-language task to automate.")


class ApprovalRequestBody(BaseModel):
    approved: bool = True
    comment: str = Field(
        default="",
        description="When declining: feedback for a revised plan. Declining without a comment cancels the workflow.",
    )
    approver: str = Field(default="api", description="Who made the decision (recorded on the run).")


class StepOut(BaseModel):
    plan_round: int
    step_id: int
    tool_name: str
    description: str
    status: str
    tool_input: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    attempts: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class WorkflowSummary(BaseModel):
    workflow_id: str
    task: str
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    success: Optional[bool] = None
    needed_approval: bool
    step_count: int
    summary: Optional[str] = None


class WorkflowDetail(WorkflowSummary):
    updated_at: datetime
    correction_rounds: int
    error: Optional[str] = None
    final_report: Optional[dict[str, Any]] = None
    pending_approval: Optional[dict[str, Any]] = None
    review: Optional[dict[str, Any]] = None
    steps: list[StepOut]


class WorkflowCreated(BaseModel):
    workflow_id: str
    status: str
    websocket_url: str
    result: Optional[WorkflowDetail] = None  # set when the request waited for the run to finish or pause


class WorkflowList(BaseModel):
    items: list[WorkflowSummary]
    total: int
    limit: int
    offset: int


class Metrics(BaseModel):
    total_workflows: int
    finished_workflows: int
    completed_workflows: int
    auto_completed_workflows: int = Field(description="Completed with no human approval.")
    needed_approval_workflows: int = Field(description="A human was asked to approve a plan (any outcome).")
    by_status: dict[str, int]
    auto_completed_pct: Optional[float] = Field(description="% of finished workflows that completed with no human approval.")
    needed_approval_pct: Optional[float] = Field(description="% of all workflows where a human was asked to approve a plan.")
    avg_steps_per_workflow: Optional[float] = Field(description="Executed steps per finished workflow, across all plan rounds.")
    avg_completion_time_seconds: Optional[float] = Field(
        description="Average wall-clock time of completed workflows, including time spent waiting for approval."
    )


def _summary_fields(row: models.WorkflowRun) -> dict[str, Any]:
    return dict(
        workflow_id=row.id,
        task=row.task,
        status=row.status,
        created_at=row.created_at,
        completed_at=row.completed_at,
        duration_seconds=row.duration_seconds,
        success=row.success,
        needed_approval=row.needed_approval,
        step_count=row.step_count,
        summary=row.summary,
    )


def workflow_summary(row: models.WorkflowRun) -> WorkflowSummary:
    return WorkflowSummary(**_summary_fields(row))


def workflow_detail(row: models.WorkflowRun) -> WorkflowDetail:
    return WorkflowDetail(
        **_summary_fields(row),
        updated_at=row.updated_at,
        correction_rounds=row.correction_rounds,
        error=row.error,
        final_report=row.final_report,
        pending_approval=row.pending_approval,
        review=row.review,
        steps=[StepOut.model_validate(s, from_attributes=True) for s in row.steps],
    )
