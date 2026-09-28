"""Mock email tool. Sent emails are logged to data/mock_apis/emails.json."""

from __future__ import annotations

from pathlib import Path

from src.schemas import ToolResult, utcnow
from src.tools import mock_store

FILE = "emails.json"


def send_email(to: str, subject: str, body: str, *, data_dir: Path | None = None) -> ToolResult:
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    if not recipients:
        return ToolResult.fail("send_email", "At least one recipient is required")
    invalid = [a for a in recipients if not mock_store.EMAIL_RE.match(a)]
    if invalid:
        return ToolResult.fail("send_email", f"Invalid email address(es): {', '.join(invalid)}")
    if not subject.strip():
        return ToolResult.fail("send_email", "Subject is empty")
    if not body.strip():
        return ToolResult.fail("send_email", "Body is empty")

    def append(emails: list) -> dict:
        email = {
            "id": f"EMAIL-{len(emails) + 1:04d}",
            "to": recipients,
            "subject": subject,
            "body": body,
            "sent_at": utcnow().isoformat(),
        }
        emails.append(email)
        return email

    email = mock_store.update(mock_store.resolve_dir(data_dir) / FILE, [], append)
    return ToolResult.ok("send_email", email_id=email["id"], to=recipients, subject=subject)
