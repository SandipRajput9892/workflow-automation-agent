"""Mock calendar tool. Events are logged to data/mock_apis/calendar.json."""

from __future__ import annotations

from datetime import datetime as dt
from pathlib import Path

from src.schemas import ToolResult, utcnow
from src.tools import mock_store

FILE = "calendar.json"


def create_event(title: str, attendees: list[str], datetime: str, *, data_dir: Path | None = None) -> ToolResult:
    """`datetime` is an ISO-8601 start time, e.g. 2026-10-06T14:00."""
    if not title.strip():
        return ToolResult.fail("create_event", "Title is empty")
    try:
        start = dt.fromisoformat(datetime)
    except ValueError:
        return ToolResult.fail("create_event", f"Invalid datetime '{datetime}'; use ISO format like 2026-10-06T14:00")
    invalid = [a for a in attendees if not mock_store.EMAIL_RE.match(a)]
    if invalid:
        return ToolResult.fail("create_event", f"Invalid attendee email(s): {', '.join(invalid)}")

    start_iso = start.isoformat(timespec="minutes")

    def append(events: list) -> dict | str:
        clash = next((e for e in events if e["datetime"] == start_iso), None)
        if clash:
            return f"Time slot {start_iso} is already booked by '{clash['title']}'"
        event = {
            "id": f"EVT-{len(events) + 1:04d}",
            "title": title,
            "attendees": attendees,
            "datetime": start_iso,
            "created_at": utcnow().isoformat(),
        }
        events.append(event)
        return event

    out = mock_store.update(mock_store.resolve_dir(data_dir) / FILE, [], append)
    if isinstance(out, str):
        return ToolResult.fail("create_event", out)
    return ToolResult.ok("create_event", event_id=out["id"], title=title, datetime=start_iso, attendees=attendees)
