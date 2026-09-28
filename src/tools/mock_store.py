"""Shared helpers for the mock tools, which persist to data/mock_apis/*.json."""

from __future__ import annotations

import re
from pathlib import Path

from src.config import get_settings
from src.json_store import load, update  # noqa: F401  (re-exported for the tool modules)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def resolve_dir(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir is not None else get_settings().mock_api_dir
