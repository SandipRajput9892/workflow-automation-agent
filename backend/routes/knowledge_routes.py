"""Company knowledge endpoints: list, read, write, delete and search policies/SOPs.

Documents are the Markdown/text files in the knowledge folder (data/knowledge/);
writes go to disk and are re-indexed immediately, so the next workflow uses them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from backend.schemas import KnowledgeDocument, KnowledgeDocumentBody, KnowledgeDocumentSummary, KnowledgeSearchResult
from src.memory.knowledge_base import InvalidDocument, KnowledgeBase

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def get_knowledge(request: Request) -> KnowledgeBase:
    return request.app.state.knowledge


@router.get("", response_model=list[KnowledgeDocumentSummary])
def list_documents(kb: KnowledgeBase = Depends(get_knowledge)) -> list[dict]:
    """Every document in the knowledge base, by file name."""
    return kb.list_documents()


@router.get("/search", response_model=list[KnowledgeSearchResult])
def search(
    q: str = Query(min_length=1, max_length=5000, description="Text to match, e.g. a task."),
    top_k: int = Query(6, ge=1, le=20),
    kb: KnowledgeBase = Depends(get_knowledge),
) -> list:
    """The passages a workflow for `q` would retrieve, nearest first."""
    return kb.search(q, top_k)


@router.get("/{name}", response_model=KnowledgeDocument)
def get_document(name: str, kb: KnowledgeBase = Depends(get_knowledge)) -> dict:
    try:
        return kb.get_document(name)
    except InvalidDocument as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Document '{name}' not found") from None


@router.put("/{name}", response_model=KnowledgeDocument)
def save_document(name: str, body: KnowledgeDocumentBody, kb: KnowledgeBase = Depends(get_knowledge)) -> dict:
    """Create or replace a document."""
    try:
        return kb.save_document(name, body.content)
    except InvalidDocument as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(name: str, kb: KnowledgeBase = Depends(get_knowledge)) -> Response:
    try:
        kb.delete_document(name)
    except InvalidDocument as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Document '{name}' not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
