from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

import pytest

from src.config import Settings
from src.memory.history_manager import AuditLog, HistoryManager
from src.memory.vector_store import WorkflowMemory
from src.tools.tool_registry import build_default_registry


class FakeLLM:
    """Scripted stand-in for ClaudeLLM.

    structured(): pops the next queued object (or exception to raise) for the
    requested schema, and records the prompt it was given.
    """

    def __init__(self) -> None:
        self.structured_queue: dict[type, deque] = defaultdict(deque)
        self.prompts: list[str] = []

    def queue(self, obj: Any) -> "FakeLLM":
        self.structured_queue[type(obj)].append(obj)
        return self

    def queue_error(self, schema: type, exc: Exception) -> "FakeLLM":
        """Make the next structured() call for `schema` raise `exc`."""
        self.structured_queue[schema].append(exc)
        return self

    def structured(self, system: str, prompt: str, schema: type) -> Any:
        self.prompts.append(prompt)
        item = self.structured_queue[schema].popleft()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        embedding_backend="hash",
        log_dir=tmp_path / "logs",
        chroma_dir=tmp_path / "chroma",
        mock_api_dir=tmp_path / "mock_apis",
        checkpoint_db=tmp_path / "checkpoints.sqlite",
    )


@pytest.fixture
def data_dir(settings):
    return settings.mock_api_dir


@pytest.fixture
def registry(data_dir):
    return build_default_registry(data_dir)


@pytest.fixture
def memory(settings) -> WorkflowMemory:
    # Chroma's EphemeralClient shares state process-wide, so give each test its own dir.
    return WorkflowMemory(settings.chroma_dir, "test_memory", embedding_backend="hash")


@pytest.fixture
def history(settings) -> HistoryManager:
    return HistoryManager(settings.log_dir)


@pytest.fixture
def audit(settings) -> AuditLog:
    return AuditLog(settings.audit_log_path)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()
