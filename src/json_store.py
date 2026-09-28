"""Small helpers for JSON files that are read-modify-written (mock APIs, audit log)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

_lock = threading.Lock()


def load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def update(path: Path, default: Any, mutate: Callable[[Any], Any]) -> Any:
    """Read-modify-write a JSON file atomically (within this process).

    `mutate` edits the loaded data in place; its return value is passed back."""
    with _lock:
        data = load(path, default)
        out = mutate(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return out
