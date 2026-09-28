"""Mock Slack tool. Messages are logged to data/mock_apis/slack.json."""

from __future__ import annotations

import re
from pathlib import Path

from src.schemas import ToolResult, utcnow
from src.tools import mock_store

FILE = "slack.json"
CHANNEL_RE = re.compile(r"^#?[a-z0-9][a-z0-9_-]{0,79}$")


def send_message(channel: str, text: str, *, data_dir: Path | None = None) -> ToolResult:
    if not CHANNEL_RE.match(channel):
        return ToolResult.fail("send_message", f"Invalid channel name '{channel}' (use lowercase, e.g. #sales)")
    if not text.strip():
        return ToolResult.fail("send_message", "Message text is empty")
    channel = "#" + channel.lstrip("#")

    def append(messages: list) -> dict:
        msg = {"id": f"MSG-{len(messages) + 1:04d}", "channel": channel, "text": text, "ts": utcnow().isoformat()}
        messages.append(msg)
        return msg

    msg = mock_store.update(mock_store.resolve_dir(data_dir) / FILE, [], append)
    return ToolResult.ok("send_message", message_id=msg["id"], channel=channel, ts=msg["ts"])
