"""Mock CRM tool. Leads are stored in data/mock_apis/crm.json."""

from __future__ import annotations

from pathlib import Path

from src.schemas import ToolResult, utcnow
from src.tools import mock_store

FILE = "crm.json"
LEAD_STATUSES = ["new", "contacted", "qualified", "proposal", "won", "lost"]


def _path(data_dir: Path | None) -> Path:
    return mock_store.resolve_dir(data_dir) / FILE


def create_lead(name: str, email: str, company: str, *, data_dir: Path | None = None) -> ToolResult:
    if not name.strip():
        return ToolResult.fail("create_lead", "Name is empty")
    if not mock_store.EMAIL_RE.match(email):
        return ToolResult.fail("create_lead", f"Invalid email address: {email}")

    def append(crm: dict) -> tuple[dict, bool]:
        leads = crm.setdefault("leads", [])
        existing = next((l for l in leads if l["email"].lower() == email.lower()), None)
        if existing:
            # One lead per email: hand back the existing record instead of failing,
            # so later steps ("<lead_id from step N>") can still use its ID.
            return existing, False
        now = utcnow().isoformat()
        lead = {
            "id": f"LEAD-{len(leads) + 1:04d}",
            "name": name,
            "email": email,
            "company": company,
            "status": "new",
            "created_at": now,
            "updated_at": now,
        }
        leads.append(lead)
        return lead, True

    lead, created = mock_store.update(_path(data_dir), {"leads": []}, append)
    return ToolResult.ok(
        "create_lead",
        lead_id=lead["id"],
        name=lead["name"],
        email=lead["email"],
        company=lead["company"],
        status=lead["status"],
        created=created,
    )


def update_status(lead_id: str, status: str, *, data_dir: Path | None = None) -> ToolResult:
    if status not in LEAD_STATUSES:
        return ToolResult.fail("update_status", f"Invalid status '{status}'. Valid: {', '.join(LEAD_STATUSES)}")

    def mutate(crm: dict) -> dict | str:
        lead = next((l for l in crm.setdefault("leads", []) if l["id"] == lead_id), None)
        if lead is None:
            return f"No lead with id {lead_id}"
        previous = lead["status"]
        lead["status"] = status
        lead["updated_at"] = utcnow().isoformat()
        return {"previous": previous}

    out = mock_store.update(_path(data_dir), {"leads": []}, mutate)
    if isinstance(out, str):
        return ToolResult.fail("update_status", out)
    return ToolResult.ok("update_status", lead_id=lead_id, previous_status=out["previous"], status=status)
