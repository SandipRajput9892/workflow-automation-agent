"""FastAPI entrypoint.

    uvicorn backend.main:app --reload --port 8000

Interactive docs at http://localhost:8000/docs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db import crud
from backend.db.database import Database
from backend.routes import knowledge_routes, metrics_routes, workflow_routes
from backend.services.workflow_service import WorkflowService
from backend.websocket import live_updates
from backend.websocket.live_updates import EventBroker
from src.config import Settings, get_settings, setup_logging

if TYPE_CHECKING:
    from src.orchestrator import WorkflowOrchestrator

logger = logging.getLogger(__name__)

OrchestratorFactory = Callable[[Settings], "WorkflowOrchestrator"]


def _default_orchestrator(settings: Settings) -> "WorkflowOrchestrator":
    from src.orchestrator import WorkflowOrchestrator

    # No approver: plans that need human sign-off pause, and are approved via the API.
    return WorkflowOrchestrator(settings, approver=None)


def create_app(settings: Settings | None = None, orchestrator_factory: OrchestratorFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    factory = orchestrator_factory or _default_orchestrator

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.backend_database_url)
        db.create_all()
        with db.session() as session:
            stale = crud.mark_stale_running(session)
        if stale:
            logger.warning("marked %d workflow(s) left running by a previous server as interrupted", stale)
        broker = EventBroker()
        broker.bind_loop(asyncio.get_running_loop())
        service = WorkflowService(factory(settings), db, broker, settings.backend_max_concurrent_runs)
        app.state.db, app.state.broker, app.state.service = db, broker, service
        app.state.knowledge = service.orchestrator.knowledge
        try:
            yield
        finally:
            service.shutdown()
            db.engine.dispose()

    app = FastAPI(
        title="Workflow Automation Agent",
        description="Run natural-language workflows across email, calendar, CRM and Slack, guided by company knowledge.",
        version="1.0.0",
        lifespan=lifespan,
    )
    allow_all = settings.cors_origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not allow_all,  # browsers reject credentials with a wildcard origin
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(workflow_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(knowledge_routes.router)
    app.include_router(live_updates.router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


setup_logging()
app = create_app()
