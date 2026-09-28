"""Registry of all tools: name, description, and Claude tool-use input schema.

The planner reads `get_tool_schemas()` to see what is available; the executor
passes the same list to Claude as `tools` and runs calls via `execute()`.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError, validate_call

from src.schemas import ToolResult
from src.tools.calendar_tool import create_event
from src.tools.crm_tool import LEAD_STATUSES, create_lead, update_status
from src.tools.email_tool import send_email
from src.tools.slack_tool import send_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: Callable[..., ToolResult]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        func: Callable[..., ToolResult],
        **bound: Any,
    ) -> None:
        """`bound` kwargs (e.g. data_dir) are fixed at registration and hidden from the model."""
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        # validate_call enforces the Python type hints, so malformed input
        # (e.g. a string where a list is expected) fails cleanly.
        call = functools.partial(validate_call(func), **bound)
        self._tools[name] = RegisteredTool(name, description, input_schema, call)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schema(self, name: str) -> dict[str, Any]:
        """Claude tool-use definition of one tool."""
        return self._tools[name].schema()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Tool definitions in Claude tool-use format. Sorted, so the list is
        byte-identical across requests (keeps the prompt cache prefix stable)."""
        return [self._tools[n].schema() for n in self.names()]

    def describe(self) -> str:
        """Tool schemas rendered for inclusion in a prompt."""
        return json.dumps(self.get_tool_schemas(), indent=2)

    def validate_input(self, name: str, tool_input: dict[str, Any]) -> list[str]:
        """Check a tool call against the tool's input_schema. Returns a list of
        human-readable problems (empty if valid). Covers the JSON Schema subset
        the tools use: required, unknown properties, type, enum, array items."""
        tool = self._tools.get(name)
        if tool is None:
            return [f"Unknown tool '{name}'. Available: {', '.join(self.names())}"]

        schema = tool.input_schema
        props: dict[str, Any] = schema.get("properties", {})
        problems = [f"Unexpected argument '{k}'" for k in sorted(set(tool_input) - set(props))]
        problems += [f"Missing required argument '{k}'" for k in schema.get("required", []) if k not in tool_input]
        for key, value in tool_input.items():
            if key in props:
                problems += _check_value(key, value, props[key])
        return problems

    def execute(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        """Run a tool by name. Never raises; every failure becomes a ToolResult."""
        problems = self.validate_input(name, tool_input)
        if problems:
            return ToolResult.fail(name, "; ".join(problems))
        tool = self._tools[name]

        try:
            result = tool.func(**tool_input)
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            result = ToolResult.fail(name, f"Invalid arguments: {errors}")
        except Exception as exc:  # a tool bug must not crash the workflow
            logger.exception("tool %s raised", name)
            result = ToolResult.fail(name, f"Internal tool error: {exc}")

        logger.info("tool %s -> %s", name, "ok" if result.success else f"failed: {result.error}")
        return result


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _check_value(path: str, value: Any, spec: dict[str, Any]) -> list[str]:
    expected = spec.get("type")
    py_type = _JSON_TYPES.get(expected) if expected else None
    # bool is a subclass of int in Python; don't let True pass as an integer.
    if py_type and (not isinstance(value, py_type) or (expected in ("integer", "number") and isinstance(value, bool))):
        return [f"'{path}' must be of type {expected}, got {type(value).__name__}"]
    if "enum" in spec and value not in spec["enum"]:
        return [f"'{path}' must be one of {spec['enum']}, got {value!r}"]
    if expected == "array" and "items" in spec:
        return [p for i, item in enumerate(value) for p in _check_value(f"{path}[{i}]", item, spec["items"])]
    return []


def build_default_registry(data_dir: Path | None = None) -> ToolRegistry:
    """Register every tool. `data_dir` overrides where mock data is written
    (defaults to settings.mock_api_dir); it is bound here and never exposed to
    the model."""

    registry = ToolRegistry()
    registry.register(
        name="send_email",
        description=(
            "Send an email. Use for any outbound email to customers, leads or colleagues. "
            "Compose a complete, professional message; the body is sent exactly as given. "
            "Returns email_id, to, subject."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient address; separate multiple addresses with commas."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Plain-text email body."},
            },
            "required": ["to", "subject", "body"],
        },
        func=send_email,
        data_dir=data_dir,
    )
    registry.register(
        name="create_event",
        description=(
            "Create a calendar event and invite attendees. Fails if another event already starts at the same time. "
            "Returns event_id, title, datetime, attendees."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee email addresses.",
                },
                "datetime": {"type": "string", "description": "Start time in ISO-8601 local time, e.g. 2026-10-06T14:00."},
            },
            "required": ["title", "attendees", "datetime"],
        },
        func=create_event,
        data_dir=data_dir,
    )
    registry.register(
        name="create_lead",
        description=(
            "Create a new lead in the CRM with status 'new'. Fails if a lead with the same email already exists. "
            "Returns lead_id (needed by update_status), name, email, company, status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contact's full name."},
                "email": {"type": "string", "description": "Contact's email address."},
                "company": {"type": "string", "description": "Company name."},
            },
            "required": ["name", "email", "company"],
        },
        func=create_lead,
        data_dir=data_dir,
    )
    registry.register(
        name="update_status",
        description=(
            "Update the pipeline status of an existing CRM lead, identified by its lead_id (e.g. LEAD-0001). "
            "Returns lead_id, previous_status, status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lead_id": {"type": "string", "description": "Lead ID returned by create_lead."},
                "status": {"type": "string", "enum": LEAD_STATUSES, "description": "New pipeline status."},
            },
            "required": ["lead_id", "status"],
        },
        func=update_status,
        data_dir=data_dir,
    )
    registry.register(
        name="send_message",
        description="Post a message to a Slack channel. Returns message_id, channel, ts.",
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name, lowercase, e.g. #sales."},
                "text": {"type": "string", "description": "Message text (Slack markdown allowed)."},
            },
            "required": ["channel", "text"],
        },
        func=send_message,
        data_dir=data_dir,
    )
    return registry
