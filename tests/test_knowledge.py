"""Company knowledge base (RAG): chunking, indexing, retrieval, and its use in workflows."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from backend.db.database import Database
from backend.main import create_app
from src.memory.knowledge_base import MAX_CHUNK_CHARS, InvalidDocument, KnowledgeBase, chunk_document
from src.orchestrator import WorkflowOrchestrator
from src.schemas import FinalReport, PlanAssessment, PlanDraft, PlannedStep, ReflectionReview, SupervisorDecision

EMAIL_DOC = """# Customer Email Guidelines

Owner: Marketing.

## Sign-off

Every external email ends with "Best regards, The OurCo Team".

## Tone

Warm, professional and concise. Address the person by first name.
"""

SLACK_DOC = """# Slack Guidelines

## Privacy

Do not post customer email addresses or phone numbers in any Slack channel.
"""

LEAD = {"name": "Priya Shah", "email": "priya@acme.com", "company": "Acme"}


@pytest.fixture
def kb(settings):
    return KnowledgeBase(settings.knowledge_dir, settings.chroma_dir, "test_knowledge", "hash", max_distance=2.0)


# ------------------------------------------------------------------ chunking


def test_chunks_by_section_with_title():
    chunks = chunk_document("email_guidelines.md", EMAIL_DOC)
    assert [(c["title"], c["section"]) for c in chunks] == [
        ("Customer Email Guidelines", "Overview"),
        ("Customer Email Guidelines", "Sign-off"),
        ("Customer Email Guidelines", "Tone"),
    ]
    assert "Best regards" in chunks[1]["text"] and "#" not in chunks[1]["text"]


def test_title_falls_back_to_file_name_and_long_sections_split():
    long = "\n\n".join(f"Paragraph {i}. " + "word " * 100 for i in range(10))
    chunks = chunk_document("travel-policy.txt", long)
    assert {c["title"] for c in chunks} == {"Travel policy"}
    assert len(chunks) > 1 and all(len(c["text"]) <= MAX_CHUNK_CHARS for c in chunks)


# ----------------------------------------------------------- knowledge base


def test_save_list_get_search_delete(kb):
    kb.save_document("email_guidelines.md", EMAIL_DOC)
    kb.save_document("slack_guidelines.md", SLACK_DOC)

    docs = kb.list_documents()
    assert [(d["name"], d["title"], d["chunks"]) for d in docs] == [
        ("email_guidelines.md", "Customer Email Guidelines", 3),
        ("slack_guidelines.md", "Slack Guidelines", 1),
    ]
    assert kb.get_document("slack_guidelines.md")["content"] == SLACK_DOC

    hits = kb.search("which sign-off should an external email end with", top_k=1)
    assert [(h.source, h.section) for h in hits] == [("email_guidelines.md", "Sign-off")]

    kb.delete_document("email_guidelines.md")
    assert [d["name"] for d in kb.list_documents()] == ["slack_guidelines.md"]
    assert {h.source for h in kb.search("email sign-off", top_k=5)} == {"slack_guidelines.md"}


def test_index_follows_files_edited_on_disk(kb, settings):
    settings.knowledge_dir.mkdir(parents=True)
    path = settings.knowledge_dir / "slack_guidelines.md"
    path.write_text(SLACK_DOC, encoding="utf-8")
    assert kb.search("slack privacy", top_k=1)[0].text.startswith("Do not post")

    path.write_text("# Slack Guidelines\n\n## Channels\n\nUse #sales for leads.\n", encoding="utf-8")
    assert [(h.section, h.text) for h in kb.search("slack", top_k=5)] == [("Channels", "Use #sales for leads.")]

    path.unlink()
    assert kb.search("slack", top_k=5) == [] and kb.list_documents() == []


def test_max_distance_filters_unrelated_passages(settings):
    strict = KnowledgeBase(settings.knowledge_dir, settings.chroma_dir, "strict", "hash", max_distance=0.0)
    strict.save_document("slack_guidelines.md", SLACK_DOC)
    assert strict.search("quarterly revenue forecast", top_k=3) == []


@pytest.mark.parametrize("name", ["../secrets.md", "notes.pdf", "a/b.md", ".hidden.md", ""])
def test_rejects_unsafe_names(kb, name):
    with pytest.raises(InvalidDocument):
        kb.save_document(name, "# X\n\ntext")


def test_rejects_empty_content(kb):
    with pytest.raises(InvalidDocument):
        kb.save_document("empty.md", "   \n")


# -------------------------------------------------------------- in workflows


def _decision(approved=True):
    return SupervisorDecision(approved=approved, risk_level="low", normalized_task="Email Priya a follow-up and tell #sales",
                              required_tools=[], reasoning="ok")


def _plan():
    return PlanDraft(goal="g", steps=[PlannedStep(id=1, description="Add lead", tool_name="create_lead",
                                                  tool_input_json=json.dumps(LEAD))])


def _finish(fake_llm):
    fake_llm.queue(PlanAssessment(verdict="approve", risk_level="low", concerns=[], feedback=""))
    fake_llm.queue(ReflectionReview(goal_achieved=True, assessment="done", issues=[], verdict="workflow_complete",
                                    corrective_steps=[]))
    fake_llm.queue(FinalReport(success=True, summary="done", actions_taken=[], open_issues=[]))


@pytest.fixture
def orch(settings, fake_llm, registry, memory, history, audit, kb):
    return WorkflowOrchestrator(settings, llm=fake_llm, registry=registry, memory=memory, history=history,
                                audit=audit, approver=None, today="2026-09-28", knowledge=kb)


def test_workflow_retrieves_knowledge_for_planner_and_gate(orch, kb, fake_llm):
    kb.save_document("email_guidelines.md", EMAIL_DOC)
    fake_llm.queue(_decision()).queue(_plan())
    _finish(fake_llm)

    events = list(orch.stream("Email Priya"))
    run = events[-1].run

    assert run.status == "completed"
    assert {k.source for k in run.knowledge} == {"email_guidelines.md"}
    knowledge_event = next(e for e in events if e.kind == "knowledge")
    assert "Customer Email Guidelines (email_guidelines.md)" in knowledge_event.message
    assert len(knowledge_event.data["snippets"]) == len(run.knowledge)
    planner_prompt, gate_prompt = fake_llm.prompts[1], fake_llm.prompts[2]
    for prompt in (planner_prompt, gate_prompt):
        assert "<company_knowledge>" in prompt and "Best regards, The OurCo Team" in prompt


def test_no_knowledge_block_when_nothing_is_indexed(orch, fake_llm):
    fake_llm.queue(_decision()).queue(_plan())
    _finish(fake_llm)

    run = orch.run("Email Priya")

    assert run.knowledge == [] and "<company_knowledge>" not in fake_llm.prompts[1]


def test_rejected_task_skips_retrieval(orch, kb, fake_llm):
    kb.save_document("email_guidelines.md", EMAIL_DOC)
    fake_llm.queue(_decision(approved=False)).queue(FinalReport(success=False, summary="no", actions_taken=[], open_issues=[]))

    events = list(orch.stream("Spam everyone"))

    assert "knowledge" not in [e.kind for e in events] and events[-1].run.knowledge == []


# ---------------------------------------------------------------------- API


@pytest.fixture
def client(settings, fake_llm, registry, memory, history, audit, kb, tmp_path):
    settings.backend_database_url = f"sqlite:///{(tmp_path / 'api.sqlite').as_posix()}"

    def factory(s):
        return WorkflowOrchestrator(s, llm=fake_llm, registry=registry, memory=memory, history=history,
                                    audit=audit, approver=None, today="2026-09-28", knowledge=kb)

    with TestClient(create_app(settings, factory)) as c:
        yield c


def test_knowledge_api_crud_and_search(client):
    assert client.get("/knowledge").json() == []

    r = client.put("/knowledge/email_guidelines.md", json={"content": EMAIL_DOC})
    assert r.status_code == 200 and r.json()["title"] == "Customer Email Guidelines" and r.json()["chunks"] == 3
    assert [d["name"] for d in client.get("/knowledge").json()] == ["email_guidelines.md"]
    assert client.get("/knowledge/email_guidelines.md").json()["content"] == EMAIL_DOC

    hits = client.get("/knowledge/search", params={"q": "email sign-off", "top_k": 1}).json()
    assert [(h["source"], h["section"]) for h in hits] == [("email_guidelines.md", "Sign-off")]

    assert client.delete("/knowledge/email_guidelines.md").status_code == 204
    assert client.get("/knowledge/email_guidelines.md").status_code == 404
    assert client.delete("/knowledge/email_guidelines.md").status_code == 404


def test_knowledge_api_rejects_bad_input(client):
    assert client.put("/knowledge/notes.pdf", json={"content": "x"}).status_code == 422
    assert client.put("/knowledge/notes.md", json={"content": ""}).status_code == 422
    assert client.get("/knowledge/..%2Fsecrets.md").status_code in (404, 422)


def test_workflow_detail_lists_knowledge_used(client, kb, fake_llm):
    kb.save_document("email_guidelines.md", EMAIL_DOC)
    fake_llm.queue(_decision()).queue(_plan())
    _finish(fake_llm)

    body = client.post("/workflows", json={"task": "Email Priya"}).json()

    used = body["result"]["knowledge"]
    assert used and {k["source"] for k in used} == {"email_guidelines.md"}
    assert set(used[0]) == {"source", "title", "section", "text", "distance"}


def test_old_database_gets_new_columns(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.sqlite').as_posix()}"
    with create_engine(url).begin() as conn:  # a workflow_runs table from before the knowledge column
        conn.execute(text("CREATE TABLE workflow_runs (id VARCHAR(64) PRIMARY KEY, task TEXT, status VARCHAR(32))"))

    db = Database(url)
    db.create_all()

    assert "knowledge" in {c["name"] for c in inspect(db.engine).get_columns("workflow_runs")}
    db.create_all()  # idempotent
    db.engine.dispose()
