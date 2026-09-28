"""Workflow endpoints: start, inspect, list, approve."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.db import crud
from backend.db.database import get_db
from backend.schemas import (
    ApprovalRequestBody,
    WorkflowCreate,
    WorkflowCreated,
    WorkflowDetail,
    WorkflowList,
    workflow_detail,
    workflow_summary,
)
from backend.services.workflow_service import WorkflowBusy, WorkflowService
from src.schemas import ApprovalDecision

router = APIRouter(prefix="/workflows", tags=["workflows"])

StatusFilter = Literal["running", "awaiting_approval", "interrupted", "completed", "failed", "rejected"]


def get_service(request: Request) -> WorkflowService:
    return request.app.state.service


async def _respond(request: Request, run_id: str, future, wait: bool) -> JSONResponse | WorkflowCreated:
    ws_url = f"/ws/workflows/{run_id}"
    if not wait:
        body = WorkflowCreated(workflow_id=run_id, status="running", websocket_url=ws_url)
        return JSONResponse(body.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    try:
        await asyncio.wrap_future(future)
    except Exception:
        pass  # recorded on the workflow row (status "interrupted"); report it below
    with request.app.state.db.session() as db:
        detail = workflow_detail(crud.get_run(db, run_id))
    return WorkflowCreated(workflow_id=run_id, status=detail.status, websocket_url=ws_url, result=detail)


@router.post("", response_model=WorkflowCreated, status_code=status.HTTP_200_OK)
async def create_workflow(
    body: WorkflowCreate,
    request: Request,
    wait: bool = Query(True, description="Wait for the workflow to finish (or pause for approval) before responding. "
                                         "With wait=false the response is 202 with the id; follow progress over the WebSocket."),
    service: WorkflowService = Depends(get_service),
):
    """Start a workflow for a natural-language task."""
    run_id, future = service.start(body.task)
    return await _respond(request, run_id, future, wait)


@router.get("/{workflow_id}", response_model=WorkflowDetail)
def get_workflow(workflow_id: str, db: Session = Depends(get_db)):
    """Full status: every step with its tool input, result and timing, plus any plan awaiting approval."""
    row = crud.get_run(db, workflow_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    return workflow_detail(row)


@router.get("", response_model=WorkflowList)
def list_workflows(
    status_filter: Optional[StatusFilter] = Query(None, alias="status"),
    created_from: Optional[date] = Query(None, alias="from", description="Created on or after this date (UTC)."),
    created_to: Optional[date] = Query(None, alias="to", description="Created on or before this date (UTC)."),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Past and current workflows, newest first."""
    start = datetime.combine(created_from, time.min, tzinfo=timezone.utc) if created_from else None
    end = datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=timezone.utc) if created_to else None
    rows, total = crud.list_runs(db, status_filter, start, end, limit, offset)
    return WorkflowList(items=[workflow_summary(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post("/{workflow_id}/approve", response_model=WorkflowCreated)
async def approve_workflow(
    workflow_id: str,
    request: Request,
    body: Optional[ApprovalRequestBody] = None,
    wait: bool = Query(True, description="Wait for the resumed workflow to finish (or pause again)."),
    service: WorkflowService = Depends(get_service),
):
    """Approve (or decline) the plan a paused workflow is waiting on, and resume it.
    Declining with a comment asks for a revised plan; declining without one cancels."""
    body = body or ApprovalRequestBody()
    decision = ApprovalDecision(approved=body.approved, comment=body.comment, approver=body.approver)
    try:
        future = service.approve(workflow_id, decision)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    except WorkflowBusy:
        raise HTTPException(status.HTTP_409_CONFLICT, "Workflow is currently running")
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return await _respond(request, workflow_id, future, wait)
