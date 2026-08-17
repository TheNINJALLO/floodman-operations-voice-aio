from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
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
}


async def test_first_capture_skips_category_and_asks_for_name(
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
        DETAILS,
        call_id="call-continuation-1",
        caller_number="+12315550101",
    )
    assert result["continuation_required"] is True
    assert result["safe_message"] == "What name should I put this under?"
    assert result["safe_message"].count("?") == 1
    assert "water" not in result["safe_message"].lower()
    db.close()


async def test_progress_continues_without_a_ready_statement(
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

    details = await service.execute(
        "capture_intake_progress",
        DETAILS,
        call_id=call_id,
        caller_number=caller_number,
    )
    assert details["next_field"] == "name"

    named = await service.execute(
        "capture_intake_progress",
        {"name": "Josh Aldrich"},
        call_id=call_id,
        caller_number=caller_number,
    )
    assert named["safe_message"] == (
        "Josh Aldrich, right?"
    )

    await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "name",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    await service.execute(
        "capture_intake_progress",
        {"email": "josh@example.com"},
        call_id=call_id,
        caller_number=caller_number,
    )
    await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "email",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "phone",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    await service.execute(
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
    assert complete["submit_required"] is False
    assert complete["submitted"] is True
    assert "within 24 hours" in complete["safe_message"]
    db.close()


def test_agent_and_config_use_silent_capture_flow(
    project_root,
) -> None:
    import yaml

    agent = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    assert "classification happens internally and silently" in (
        agent.lower()
    )
    assert "never announce an internal service category" in (
        agent.lower()
    )

    config = yaml.safe_load(
        (
            project_root
            / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert "floodman_classify_service" not in tools
    assert tools["floodman_capture_intake_progress"]["enabled"] is True
    assert tools["floodman_submit_intake"]["enabled"] is True
