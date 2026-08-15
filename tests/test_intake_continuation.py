from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
from app.intake_flow import next_intake_state
from app.roomflow.client import RoomflowClient


DETAILS = {
    "description": (
        "A water heater leaked into the utility room "
        "and wet the drywall."
    ),
    "property_context": "Residential property.",
    "timing_summary": "Started this morning; source is off.",
    "safety_summary": (
        "No sewage, electrical contact, structural danger, "
        "or access concern reported."
    ),
    "service_requested": "water damage restoration",
    "service_key": "water_damage_restoration",
    "service_status": "supported",
}


def test_next_question_is_one_field_at_a_time() -> None:
    snapshot = {
        "caller_number": "+12315550100",
        "phone": "+12315550100",
        **DETAILS,
        "metadata": {"contact_flow_enabled": True},
    }
    state = next_intake_state(snapshot)
    assert state["field"] == "name"
    assert state["next_question"] == "What is your full name?"
    assert state["next_question"].count("?") == 1


async def test_classification_always_returns_one_question(
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
            "requested_service": "water damage restoration",
            "description": (
                "A water heater leaked into the basement "
                "utility room."
            ),
        },
        call_id="call-continuation-1",
        caller_number="+12315550101",
    )
    assert result["service_status"] == "supported"
    assert result["continuation_required"] is True
    assert result["next_question"].endswith("?")
    assert result["next_question"].count("?") == 1
    db.close()


async def test_progress_confirms_name_email_phone_address(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    call_id = "call-continuation-2"
    caller_number = "+12315550102"

    await service.execute(
        "classify_service",
        {
            "requested_service": "water damage restoration",
            "description": DETAILS["description"],
        },
        call_id=call_id,
        caller_number=caller_number,
    )

    details = await service.execute(
        "capture_intake_progress",
        DETAILS,
        call_id=call_id,
        caller_number=caller_number,
    )
    assert details["next_field"] == "name"
    assert details["stage"] == "collect_contact"

    named = await service.execute(
        "capture_intake_progress",
        {"name": "Josh Aldrich"},
        call_id=call_id,
        caller_number=caller_number,
    )
    assert named["next_field"] == "name"
    assert named["stage"] == "confirm_contact"

    name_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "name",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert name_ok["next_field"] == "email"
    assert name_ok["stage"] == "collect_contact"

    emailed = await service.execute(
        "capture_intake_progress",
        {"email": "josh@example.com"},
        call_id=call_id,
        caller_number=caller_number,
    )
    assert emailed["next_field"] == "email"
    assert emailed["stage"] == "confirm_contact"

    email_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "email",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert email_ok["next_field"] == "phone"
    assert email_ok["stage"] == "confirm_contact"

    phone_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "phone",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert phone_ok["next_field"] == "address"
    assert phone_ok["stage"] == "collect_contact"

    addressed = await service.execute(
        "capture_intake_progress",
        {
            "address": (
                "8805 East Melendy Street, "
                "Ludington, MI 49431"
            )
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert addressed["next_field"] == "address"
    assert addressed["stage"] == "confirm_contact"

    complete = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "address",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert complete["ready_to_submit"] is True
    assert complete["next_question"] == ""
    db.close()


def test_agent_and_tools_forbid_grouped_questions(
    project_root,
) -> None:
    import yaml

    agent = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    assert "Ask exactly one question per turn" in agent
    assert "name, then email, then phone, then address" in agent
    assert "immediately ask next_question" in agent

    config = yaml.safe_load(
        (
            project_root
            / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert "Non-terminal" in tools[
        "floodman_classify_service"
    ]["description"]
    assert "exactly one" in tools[
        "floodman_capture_intake_progress"
    ]["description"].lower()
