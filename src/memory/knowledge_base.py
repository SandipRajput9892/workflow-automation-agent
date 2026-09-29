"""Company knowledge base (RAG): policies, SOPs and guidelines.

Documents are plain Markdown or text files in settings.knowledge_dir
(data/knowledge/ by default). Each file is split into passages, one per "##"
section (long sections are split further by paragraph), and embedded into a
ChromaDB collection next to the workflow memory.

The index follows the folder: every search (and every write through this
class) re-indexes files whose content changed and drops files that were
deleted, so editing a file on disk is enough.

For each task, the orchestrator retrieves the most relevant passages once at
intake and shows them to the planner and the safety gate.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb

from src.memory.vector_store import HashEmbeddingFunction
from src.schemas import KnowledgeSnippet

logger = logging.getLogger(__name__)

SUFFIXES = (".md", ".txt")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(md|txt)$")
MAX_DOC_CHARS = 200_000
MAX_CHUNK_CHARS = 1200


class InvalidDocument(ValueError):
    """Bad document name or content."""


class KnowledgeBase:
    def __init__(
        self,
        docs_dir: Path,
        persist_dir: Path | None,
        collection: str = "company_knowledge",
        embedding_backend: str = "default",
        max_distance: float = 0.8,
    ):
        """persist_dir=None gives an in-memory index (note: Chroma shares it process-wide).
        Passages farther than `max_distance` (cosine distance) are not returned."""
        self.docs_dir = Path(docs_dir)
        self.max_distance = max_distance
        client = chromadb.PersistentClient(path=str(persist_dir)) if persist_dir else chromadb.EphemeralClient()
        kwargs: dict[str, Any] = {"metadata": {"hnsw:space": "cosine"}}
        if embedding_backend == "hash":
            kwargs["embedding_function"] = HashEmbeddingFunction()
        self._collection = client.get_or_create_collection(collection, **kwargs)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ retrieval

    def search(self, query: str, top_k: int = 4) -> list[KnowledgeSnippet]:
        """Passages most relevant to `query`, nearest first."""
        if not query.strip() or top_k <= 0:
            return []
        with self._lock:
            self.sync()
            total = self._collection.count()
            if total == 0:
                return []
            res = self._collection.query(query_texts=[query], n_results=min(top_k, total))
        return [
            KnowledgeSnippet(
                source=meta["source"], title=meta["title"], section=meta.get("section", ""), text=meta["text"], distance=dist
            )
            for meta, dist in zip(res["metadatas"][0], res["distances"][0])
            if dist <= self.max_distance
        ]

    def sync(self) -> int:
        """Bring the index in line with the folder. Returns the number of files re-indexed."""
        with self._lock:
            indexed = self._indexed_hashes()
            files = self._files()
            changed = 0
            for name, path in files.items():
                text = _read(path)
                digest = _hash(text)
                if indexed.get(name) != digest:
                    self._index(name, text, digest)
                    changed += 1
            for name in indexed.keys() - files.keys():
                self._collection.delete(where={"source": name})
                logger.info("knowledge: removed %s from the index", name)
            return changed

    # ------------------------------------------------------------ documents

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            self.sync()
            chunks: dict[str, int] = {}
            for meta in self._collection.get(include=["metadatas"])["metadatas"]:
                chunks[meta["source"]] = chunks.get(meta["source"], 0) + 1
            return [self._describe(name, path, chunks.get(name, 0)) for name, path in sorted(self._files().items())]

    def get_document(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        text = _read(path)
        return {**self._describe(name, path, len(chunk_document(name, text))), "content": text}

    def save_document(self, name: str, content: str) -> dict[str, Any]:
        """Create or overwrite a document and index it."""
        path = self._path(name)
        content = content.replace("\r\n", "\n").strip() + "\n"
        if not content.strip():
            raise InvalidDocument("Document is empty")
        if len(content) > MAX_DOC_CHARS:
            raise InvalidDocument(f"Document is too long ({len(content):,} characters; the limit is {MAX_DOC_CHARS:,})")
        with self._lock:
            self.docs_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self._index(name, content, _hash(content))
        return self.get_document(name)

    def delete_document(self, name: str) -> None:
        path = self._path(name)
        with self._lock:
            if not path.is_file():
                raise FileNotFoundError(name)
            path.unlink()
            self._collection.delete(where={"source": name})

    # ------------------------------------------------------------ internals

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise InvalidDocument(
                "Document names use letters, digits, '-' and '_' and end in .md or .txt, e.g. email_guidelines.md"
            )
        return self.docs_dir / name

    def _files(self) -> dict[str, Path]:
        if not self.docs_dir.is_dir():
            return {}
        return {
            p.name: p for p in self.docs_dir.iterdir()
            if p.is_file() and p.suffix.lower() in SUFFIXES and NAME_RE.match(p.name)
        }

    def _indexed_hashes(self) -> dict[str, str]:
        metas = self._collection.get(include=["metadatas"])["metadatas"]
        return {m["source"]: m["doc_hash"] for m in metas}

    def _index(self, name: str, text: str, digest: str) -> None:
        self._collection.delete(where={"source": name})
        chunks = chunk_document(name, text)
        if chunks:
            self._collection.add(
                ids=[f"{name}::{i}" for i in range(len(chunks))],
                documents=[f"{c['title']} - {c['section']}\n{c['text']}" for c in chunks],
                metadatas=[{**c, "source": name, "doc_hash": digest} for c in chunks],
            )
        logger.info("knowledge: indexed %s (%d passages)", name, len(chunks))

    @staticmethod
    def _describe(name: str, path: Path, chunks: int) -> dict[str, Any]:
        stat = path.stat()
        return {
            "name": name,
            "title": document_title(name, _read(path)),
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "chunks": chunks,
        }


# ---------------------------------------------------------------- chunking


def document_title(name: str, text: str) -> str:
    """The first "# " heading, or a title made from the file name."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            break
    return Path(name).stem.replace("_", " ").replace("-", " ").strip().capitalize()


def chunk_document(name: str, text: str) -> list[dict[str, str]]:
    """Split a document into passages: one per "##"/"###" section, with long
    sections split at paragraph boundaries. Each passage is
    {"title", "section", "text"}."""
    title = document_title(name, text)
    sections: list[tuple[str, list[str]]] = [("Overview", [])]
    for line in text.splitlines():
        if line.startswith("# ") and line[2:].strip() == title:
            continue
        heading = re.match(r"^#{2,3}\s+(.*)", line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        else:
            sections[-1][1].append(line)

    chunks = []
    for section, lines in sections:
        for part in _split("\n".join(lines).strip()):
            chunks.append({"title": title, "section": section, "text": part})
    return chunks


def _split(text: str) -> list[str]:
    if not text:
        return []
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) + 2 > MAX_CHUNK_CHARS:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
        while len(current) > MAX_CHUNK_CHARS:  # a single huge paragraph
            parts.append(current[:MAX_CHUNK_CHARS])
            current = current[MAX_CHUNK_CHARS:]
    if current.strip():
        parts.append(current)
    return parts


def format_knowledge(snippets: list[KnowledgeSnippet]) -> str:
    """Retrieved passages rendered for a prompt."""
    return "\n\n".join(
        f"[{i}] {s.title}{f' > {s.section}' if s.section else ''} ({s.source})\n{s.text}"
        for i, s in enumerate(snippets, 1)
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
