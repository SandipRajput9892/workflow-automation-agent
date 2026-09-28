"""Runs orchestrator workflows in worker threads and mirrors their progress
into the database (step logs) and the live-update broker (WebSockets)."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from backend.db import crud
from backend.db.database import Database
from backend.schemas import workflow_detail
from backend.websocket.live_updates import EventBroker
from src.orchestrator import WorkflowOrchestrator
from src.schemas import ApprovalDecision, ProgressEvent, WorkflowRun

logger = logging.getLogger(__name__)


class WorkflowBusy(Exception):
    """The workflow is currently executing."""


class WorkflowService:
    def __init__(self, orchestrator: WorkflowOrchestrator, db: Database, broker: EventBroker, max_workers: int = 4):
        # The orchestrator must NOT have an approver: runs that need approval
        # pause, and are resumed through the API.
        self.orchestrator = orchestrator
        self.db = db
        self.broker = broker
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="workflow")
        self._active: dict[str, Future] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------------- public

    def start(self, task: str) -> tuple[str, Future]:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self.db.session() as db:
            crud.create_run(db, run_id, task)
        return run_id, self._submit(run_id, lambda on_event: self.orchestrator.run(task, run_id=run_id, on_event=on_event))

    def approve(self, run_id: str, decision: ApprovalDecision) -> Future:
        """Resume a paused run with a human decision. Raises KeyError / ValueError / WorkflowBusy."""
        if self.is_active(run_id):
            raise WorkflowBusy(run_id)
        run = self.orchestrator.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status != "awaiting_approval":
            raise ValueError(f"Workflow is {run.status}, not awaiting approval")
        with self.db.session() as db:
            crud.set_status(db, run_id, "running")
        return self._submit(run_id, lambda on_event: self.orchestrator.resume(run_id, decision, on_event=on_event))

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- internals

    def _submit(self, run_id: str, work: Callable[[Callable[[ProgressEvent], None]], WorkflowRun]) -> Future:
        with self._lock:
            if run_id in self._active:
                raise WorkflowBusy(run_id)
            future = self._pool.submit(self._execute, run_id, work)
            self._active[run_id] = future
        future.add_done_callback(lambda _: self._forget(run_id))
        return future

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._active.pop(run_id, None)

    def _execute(self, run_id: str, work: Callable) -> WorkflowRun:
        try:
            return work(lambda event: self._on_event(run_id, event))
        except Exception as exc:  # the orchestrator handles its own errors; this is a last resort
            logger.exception("workflow %s crashed outside the orchestrator", run_id)
            with self.db.session() as db:
                crud.set_status(db, run_id, "interrupted", error=repr(exc))
            self.broker.publish(run_id, {"kind": "run_interrupted", "workflow_id": run_id, "message": repr(exc)})
            raise

    def _on_event(self, run_id: str, event: ProgressEvent) -> None:
        message = event.model_dump(mode="json", exclude={"run"})
        message["workflow_id"] = run_id
        try:
            with self.db.session() as db:
                if event.run is not None:  # final event of this run()/resume() call
                    row = crud.sync_run(db, event.run)
                    db.flush()
                    db.refresh(row)
                    message["workflow"] = workflow_detail(row).model_dump(mode="json")
                elif event.kind in ("step_started", "step_finished"):
                    crud.record_step_event(db, run_id, event.kind, event.data)
                elif event.kind == "awaiting_approval":
                    crud.set_status(db, run_id, "awaiting_approval")
        except Exception:  # never let bookkeeping break the workflow itself
            logger.exception("failed to record event %s for %s", event.kind, run_id)
        self.broker.publish(run_id, message)
