import json

from src.tools.calendar_tool import create_event
from src.tools.crm_tool import create_lead, update_status
from src.tools.email_tool import send_email
from src.tools.slack_tool import send_message


def _read(data_dir, name):
    return json.loads((data_dir / name).read_text(encoding="utf-8"))


# --- individual tools --------------------------------------------------------


def test_send_email_logs_to_file(data_dir):
    res = send_email("tom@globex.io, ann@globex.io", "Hello", "Body text", data_dir=data_dir)
    assert res.success and res.data["email_id"] == "EMAIL-0001"
    emails = _read(data_dir, "emails.json")
    assert emails[0]["to"] == ["tom@globex.io", "ann@globex.io"]
    assert emails[0]["subject"] == "Hello"


def test_send_email_validation(data_dir):
    res = send_email("not-an-email", "Hi", "Body", data_dir=data_dir)
    assert not res.success and "Invalid email" in res.error
    assert not send_email("a@b.com", "", "Body", data_dir=data_dir).success
    assert not (data_dir / "emails.json").exists()


def test_create_event_and_conflict(data_dir):
    res = create_event("Demo", ["tom@globex.io"], "2026-10-06T14:00", data_dir=data_dir)
    assert res.success and res.data["event_id"] == "EVT-0001"
    clash = create_event("Other", [], "2026-10-06T14:00:00", data_dir=data_dir)
    assert not clash.success and "already booked" in clash.error
    assert len(_read(data_dir, "calendar.json")) == 1


def test_create_event_validation(data_dir):
    assert "Invalid datetime" in create_event("X", [], "next tuesday", data_dir=data_dir).error
    assert "Invalid attendee" in create_event("X", ["bob"], "2026-10-06T14:00", data_dir=data_dir).error


def test_crm_create_and_update(data_dir):
    lead = create_lead("Priya Shah", "priya@acme.com", "Acme", data_dir=data_dir)
    assert lead.success and lead.data == {
        "lead_id": "LEAD-0001", "name": "Priya Shah", "email": "priya@acme.com", "company": "Acme", "status": "new",
    }
    dup = create_lead("Priya S", "PRIYA@acme.com", "Acme", data_dir=data_dir)
    assert not dup.success and "already exists" in dup.error

    upd = update_status("LEAD-0001", "qualified", data_dir=data_dir)
    assert upd.success and upd.data["previous_status"] == "new"
    assert _read(data_dir, "crm.json")["leads"][0]["status"] == "qualified"

    assert "Invalid status" in update_status("LEAD-0001", "hot", data_dir=data_dir).error
    assert "No lead" in update_status("LEAD-9999", "won", data_dir=data_dir).error


def test_send_message(data_dir):
    res = send_message("sales", "Deal closed!", data_dir=data_dir)
    assert res.success and res.data["channel"] == "#sales"
    assert _read(data_dir, "slack.json")[0]["text"] == "Deal closed!"
    assert not send_message("#Bad Channel", "hi", data_dir=data_dir).success
    assert not send_message("#sales", "   ", data_dir=data_dir).success


# --- registry ------------------------------------------------------------------


def test_registry_schemas_are_claude_tool_format(registry):
    schemas = registry.get_tool_schemas()
    assert [s["name"] for s in schemas] == ["create_event", "create_lead", "send_email", "send_message", "update_status"]
    for s in schemas:
        assert set(s) == {"name", "description", "input_schema"}
        assert s["input_schema"]["type"] == "object"
        assert set(s["input_schema"]["required"]) <= set(s["input_schema"]["properties"])
        assert "data_dir" not in s["input_schema"]["properties"]
    assert json.loads(registry.describe()) == schemas


def test_registry_execute_routes_to_data_dir(registry, data_dir):
    res = registry.execute("send_message", {"channel": "#general", "text": "hi"})
    assert res.success
    assert (data_dir / "slack.json").exists()


def test_registry_rejects_bad_calls(registry):
    assert "Unknown tool" in registry.execute("delete_everything", {}).error
    assert "Missing required" in registry.execute("send_email", {"to": "a@b.com"}).error
    # the model must not be able to redirect where data is written
    res = registry.execute("send_message", {"channel": "#x", "text": "hi", "data_dir": "C:/"})
    assert not res.success and "Unexpected argument" in res.error
    # type validation via pydantic
    res = registry.execute("create_event", {"title": "T", "attendees": "a@b.com", "datetime": "2026-10-06T10:00"})
    assert not res.success and "'attendees' must be of type array" in res.error
