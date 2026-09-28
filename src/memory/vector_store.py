"""Long-term workflow memory backed by ChromaDB.

Each finished workflow is stored as one embedded document combining the task
description, the final plan (the tool calls that were actually made) and the
outcome, so a new task can be matched against everything about past runs. The
full Plan is kept as JSON in the record's metadata so it can be returned as a
Plan object, not just text.

Use the functions in src.memory.history_manager (save_workflow_run /
get_similar_past_workflows) rather than this class directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings

from src.schemas import PastWorkflow, Plan

logger = logging.getLogger(__name__)

MAX_VALUE_CHARS = 120  # long tool-input values (e.g. email bodies) are clipped in the embedded text


class HashEmbeddingFunction(EmbeddingFunction[Documents]):
    """Deterministic, dependency-free bag-of-words embedding (feature hashing).

    Much weaker than a neural model but needs no download, so it is used for
    tests and offline environments (EMBEDDING_BACKEND=hash).
    """

    def __init__(self, dim: int = 384):
        self.dim = dim

    def __call__(self, input: Documents) -> Embeddings:
        vectors = []
        for text in input:
            vec = [0.0] * self.dim
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(token.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors

    @staticmethod
    def name() -> str:
        return "workflow-hash-embedding"

    def get_config(self) -> dict[str, Any]:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "HashEmbeddingFunction":
        return HashEmbeddingFunction(**config)


class WorkflowMemory:
    def __init__(
        self,
        persist_dir: Path | None,
        collection: str = "workflow_memory",
        embedding_backend: str = "default",
        max_distance: float = 0.6,
    ):
        """persist_dir=None gives an in-memory store (note: Chroma shares it process-wide).
        Matches farther than `max_distance` (cosine distance, 0 = identical) are dropped."""
        self._client = chromadb.PersistentClient(path=str(persist_dir)) if persist_dir else chromadb.EphemeralClient()
        self._name = collection
        self._kwargs: dict[str, Any] = {"metadata": {"hnsw:space": "cosine"}}
        if embedding_backend == "hash":
            self._kwargs["embedding_function"] = HashEmbeddingFunction()
        self.max_distance = max_distance
        self._collection = self._client.get_or_create_collection(collection, **self._kwargs)

    def add(self, run_id: str, task: str, plan: Plan, outcome: str, success: bool, status: str) -> None:
        """Store (or overwrite) one workflow."""
        self._collection.upsert(
            ids=[run_id],
            documents=[workflow_document(task, plan, outcome, status)],
            metadatas=[{
                "task": task,
                "plan_json": plan.model_dump_json(),
                "outcome": outcome[:2000],
                "success": success,
                "status": status,
                "created_at": time.time(),
            }],
        )
        logger.info("memory: stored workflow %s (%s)", run_id, status)

    def search(self, task: str, top_k: int = 3, successful_only: bool = True) -> list[PastWorkflow]:
        """Past workflows most similar to `task`, nearest first."""
        total = self._collection.count()
        if total == 0 or not task.strip() or top_k <= 0:
            return []
        where = {"success": True} if successful_only else None
        try:
            res = self._collection.query(query_texts=[task], n_results=min(top_k, total), where=where)
        except Exception as exc:  # e.g. the filter matches nothing in some Chroma versions
            logger.warning("memory search failed: %s", exc)
            return []
        return [
            PastWorkflow(
                run_id=rid,
                task=meta["task"],
                plan=Plan.model_validate_json(meta["plan_json"]),
                outcome=meta["outcome"],
                success=bool(meta["success"]),
                status=meta["status"],
                distance=dist,
            )
            for rid, meta, dist in zip(res["ids"][0], res["metadatas"][0], res["distances"][0])
            if dist <= self.max_distance and "plan_json" in meta  # skip records from older formats
        ]

    def count(self) -> int:
        return self._collection.count()

    def clear(self) -> None:
        self._client.delete_collection(self._name)
        self._collection = self._client.get_or_create_collection(self._name, **self._kwargs)


def workflow_document(task: str, plan: Plan, outcome: str, status: str) -> str:
    """The text that gets embedded: task + final plan + outcome."""
    return f"Task: {task}\nPlan:\n{render_plan(plan)}\nOutcome ({status}): {outcome}"


def render_plan(plan: Plan) -> str:
    return "\n".join(f"{s.id}. {s.tool_name}({_clip(s.tool_input)}): {s.description}" for s in plan.steps)


def _clip(tool_input: dict[str, Any]) -> str:
    return json.dumps(
        {k: v[:MAX_VALUE_CHARS] + "..." if isinstance(v, str) and len(v) > MAX_VALUE_CHARS else v for k, v in tool_input.items()},
        default=str,
    )
