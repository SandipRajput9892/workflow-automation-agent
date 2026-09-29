"""SQLAlchemy models: one WorkflowRun row per workflow, one StepLog row per executed step."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # same as the orchestrator's run_id
    task: Mapped[str] = mapped_column(Text)
    # running | awaiting_approval | interrupted | completed | failed | rejected
    status: Mapped[str] = mapped_column(String(32), index=True, default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)  # created -> finished, incl. time waiting for approval

    success: Mapped[Optional[bool]] = mapped_column(Boolean)
    needed_approval: Mapped[bool] = mapped_column(Boolean, default=False)  # a human was (or is being) asked
    step_count: Mapped[int] = mapped_column(Integer, default=0)  # executed steps across all plan rounds
    correction_rounds: Mapped[int] = mapped_column(Integer, default=0)

    summary: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    final_report: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    pending_approval: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)  # plan + gate review while paused
    review: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)  # reflection agent's last verdict
    knowledge: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)  # company knowledge retrieved at intake

    steps: Mapped[list["StepLog"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="(StepLog.plan_round, StepLog.step_id)",
    )


class StepLog(Base):
    __tablename__ = "step_logs"
    __table_args__ = (UniqueConstraint("workflow_id", "plan_round", "step_id", name="uq_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    plan_round: Mapped[int] = mapped_column(Integer, default=0)  # 0 = original plan, 1+ = corrective/replanned
    step_id: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # pending | running | completed | failed | skipped
    status: Mapped[str] = mapped_column(String(16), default="pending")
    tool_input: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)  # the last ToolResult
    summary: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # tool calls made (2 = retried once)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[WorkflowRun] = relationship(back_populates="steps")
