"""Real-time workflow updates over WebSocket.

    ws://localhost:8000/ws/workflows/{workflow_id}

On connect the server sends a `snapshot` message (the workflow's current state
with all step logs), then every progress event emitted while the orchestrator
works (intake, plan_created, gate, step_started, step_finished, reflection, ...)
as JSON. Events already emitted before the client connected are replayed first,
so connecting right after `POST /workflows?wait=false` misses nothing.

The socket closes after the final event of a finished run (run_completed /
run_failed / run_rejected). A paused run (run_paused) keeps the socket open:
approving it via the API resumes execution and updates keep flowing.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.db import crud
from backend.schemas import workflow_detail

router = APIRouter()

FINAL_KINDS = {"run_completed", "run_failed", "run_rejected"}


class EventBroker:
    """Fan-out of progress events from worker threads to WebSocket subscribers.

    publish() is thread-safe and may be called from any thread; subscribers
    are asyncio queues living on the server's event loop. A bounded history per
    run is kept for replay to late subscribers."""

    def __init__(self, history_per_run: int = 500, max_runs: int = 200):
        self._lock = threading.Lock()
        self._history: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._history_per_run = history_per_run
        self._max_runs = max_runs
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, run_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            history = self._history.get(run_id)
            if history is None:
                history = self._history[run_id] = deque(maxlen=self._history_per_run)
                while len(self._history) > self._max_runs:
                    self._history.popitem(last=False)
            history.append(message)
            subscribers = list(self._subscribers.get(run_id, ()))
        if self._loop is not None:
            for queue in subscribers:
                self._loop.call_soon_threadsafe(queue.put_nowait, message)

    def subscribe(self, run_id: str) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        """Register a subscriber; returns its queue and the events published so far.
        Both happen under one lock, so no event is missed or duplicated."""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
            replay = list(self._history.get(run_id, ()))
        return queue, replay

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    del self._subscribers[run_id]

    def knows(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._history


@router.websocket("/ws/workflows/{workflow_id}")
async def workflow_updates(websocket: WebSocket, workflow_id: str) -> None:
    app = websocket.app
    broker: EventBroker = app.state.broker
    with app.state.db.session() as db:
        row = crud.get_run(db, workflow_id)
        snapshot = workflow_detail(row).model_dump(mode="json") if row else None
    await websocket.accept()
    if snapshot is None and not broker.knows(workflow_id):
        # Closing after accept() so clients see the 4404 code; closing before
        # the handshake would surface only as a bare HTTP 403.
        await websocket.close(code=4404, reason="Unknown workflow")
        return

    queue, replay = broker.subscribe(workflow_id)
    try:
        await websocket.send_json({"kind": "snapshot", "workflow_id": workflow_id, "workflow": snapshot})
        for message in replay:
            await websocket.send_json(message)
        finished = any(m.get("kind") in FINAL_KINDS for m in replay) or (
            snapshot is not None
            and snapshot["status"] in crud.TERMINAL
            and not app.state.service.is_active(workflow_id)
        )
        while not finished:
            message = await queue.get()
            await websocket.send_json(message)
            finished = message.get("kind") in FINAL_KINDS
        await websocket.close()
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(workflow_id, queue)
