"""Aggregate workflow metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.db import crud
from backend.db.database import get_db
from backend.schemas import Metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_model=Metrics)
def get_metrics(db: Session = Depends(get_db)) -> Metrics:
    """Automation rate, approval rate, average steps and average completion time.
    Percentages and averages are null until there is data to compute them from."""
    return Metrics(**crud.metrics(db))
