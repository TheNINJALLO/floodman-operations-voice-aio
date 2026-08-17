from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
from app.intake_flow import next_intake_state
from app.roomflow.client import RoomflowClient


DETAILS = {
    "property_context": "Residential property; caller is the owner.",
    "timing_summary": "The leak started this morning.",
    "safety_summary": "No immediate electrical, sewage, structural, or access concern.",
}


async def test_supported_issue_classifies_silently_and_asks_next_question(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    result = await service.execute(
        "capture_intake_progress",
        {
            "service_requested": "water damage restoration",
            "description": (
                "A water heater leak caused water damage in the "
                "basement utility room."
            ),
            **DETAILS,
        },
        call_id="call-fluid-supported",
        caller_number="+12315550101",
    )

    assert result["ok"] is True
    assert result["safe_message"] == "What name should I put this under?"
    assert result["safe_message"].count("?") == 1
    lowered = result["safe_message"].lower()
    assert "water leak repair" not in lowered
    assert "water damage restoration" not in lowered
    assert "service_key" not in result
    assert "service_status" not in result
    assert "missing" not in result

    saved = db.get_call_intake("call-fluid-supported")
    assert saved is not None
    assert saved["service_status"] == "supported"
    assert saved["service_key"]
    db.close()


async def test_unsupported_notice_is_generic_and_intake_continues(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    call_id = "call-fluid-unsupported"

    first = await service.execute(
        "capture_intake_progress",
        {
            "service_requested": "roof repair",
            "description": "Wind removed shingles over a bedroom.",
            **DETAILS,
        },
        call_id=call_id,
        caller_number="+12315550102",
    )
    assert "not a service floodman offers" in (
        first["safe_message"].lower()
    )
    assert first["safe_message"].endswith(
        "What name should I put this under?"
    )
    assert first["safe_message"].count("?") == 1
    assert "roof repair" not in first["safe_message"].lower()
    assert "roofing" not in first["safe_message"].lower()

    second = await service.execute(
        "capture_intake_progress",
        {"name": "Josh Aldrich"},
        call_id=call_id,
        caller_number="+12315550102",
    )
    assert second["safe_message"] == (
        "Josh Aldrich, right?"
    )
    assert "does not currently offer" not in (
        second["safe_message"].lower()
    )

    saved = db.get_call_intake(call_id)
    assert saved is not None
    assert saved["service_status"] == "unsupported"
    db.close()


async def test_compatibility_classifier_does_not_expose_service_name(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    result = await service.execute(
        "classify_service",
        {
            "requested_service": "water leak repair",
            "description": "A water heater leak damaged a finished room.",
            **DETAILS,
        },
        call_id="call-fluid-compat",
        caller_number="+12315550103",
    )
    assert result["service_status"] == "supported"
    assert "service_name" not in result
    assert "service_reason" not in result
    assert "Water Damage Restoration" not in result["safe_message"]
    assert "water leak repair" not in result["safe_message"].lower()
    db.close()


def test_ready_state_has_no_extra_spoken_pause() -> None:
    snapshot = {
        "description": "A water line leaked into the utility room.",
        "service_requested": "water damage restoration",
        "service_key": "water_damage_restoration",
        "service_status": "supported",
        "property_context": "Residential property; caller owns it.",
        "timing_summary": "Started this morning; source is off.",
        "safety_summary": "No immediate safety concern.",
        "name": "Josh Aldrich",
        "email": "josh@example.com",
        "phone": "+12315550104",
        "caller_number": "+12315550104",
        "address": "8805 East Melendy Street, Ludington, MI 49431",
        "metadata": {
            "contact_flow_enabled": True,
            "contact_confirmations": {
                "name": "Josh Aldrich",
                "email": "josh@example.com",
                "phone": "+12315550104",
                "address": (
                    "8805 East Melendy Street, "
                    "Ludington, MI 49431"
                ),
            },
        },
    }
    state = next_intake_state(snapshot)
    assert state["ready_to_submit"] is True
    assert state["next_question"] == ""
    assert state["safe_message"] == ""


def test_inbound_agent_has_no_classification_turn(project_root) -> None:
    source = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    inbound = source.split(
        'slug="floodman_inbound"',
        1,
    )[1].split(
        'slug="floodman_google_business"',
        1,
    )[0]

    assert '"floodman_classify_service"' not in inbound
    assert "classification happens internally and silently" in (
        inbound.lower()
    )
    assert "never announce an internal service category" in (
        inbound.lower()
    )
    assert "submits the completed intake automatically" in (
        inbound.lower()
    )
    assert '"floodman_submit_intake"' not in inbound


def test_classification_tool_is_removed_from_voice_config(
    project_root,
) -> None:
    import yaml

    config = yaml.safe_load(
        (
            project_root
            / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert "floodman_classify_service" not in tools

    capture = tools["floodman_capture_intake_progress"]
    body = capture["body_template"]
    assert "{service_key}" not in body
    assert "{service_status}" not in body
    assert "{service_reason}" not in body

    names = {
        parameter["name"]
        for parameter in capture["parameters"]
    }
    assert "service_key" not in names
    assert "service_status" not in names
    assert "service_reason" not in names
    assert "classify the request silently" in (
        capture["description"].lower()
    )
