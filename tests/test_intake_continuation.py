from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
from app.intake import (
    intake_missing_fields,
    next_intake_question,
)
from app.roomflow.client import RoomflowClient


def test_next_question_orders_details_before_contact() -> None:
    snapshot = {
        "caller_number": "+12315550100",
        "phone": "+12315550100",
        "description": "Water reached the basement floor.",
        "service_status": "supported",
        "service_key": "water_damage_restoration",
    }
    missing = intake_missing_fields(snapshot)
    assert "property type and caller relationship" in missing
    assert (
        "when the issue began and whether it is active"
        in missing
    )
    assert "safety and access concerns" in missing
    question = next_intake_question(
        snapshot,
        service_questions=(
            "Is water still entering or rising?",
        ),
    )
    assert question == "Is water still entering or rising?"


async def test_classification_always_returns_a_question(
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
            "requested_service": (
                "water damage restoration"
            ),
            "description": (
                "A water heater leaked into the "
                "basement utility room."
            ),
        },
        call_id="call-continuation-1",
        caller_number="+12315550101",
    )
    assert result["service_status"] == "supported"
    assert result["continuation_required"] is True
    assert result["next_question"].endswith("?")
    assert result["safe_message"].endswith("?")
    db.close()


async def test_progress_result_drives_each_next_question(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    await service.execute(
        "classify_service",
        {
            "requested_service": (
                "water damage restoration"
            ),
            "description": (
                "A water heater leaked into the "
                "basement utility room."
            ),
        },
        call_id="call-continuation-2",
        caller_number="+12315550102",
    )

    details = await service.execute(
        "capture_intake_progress",
        {
            "description": (
                "A water heater leaked into the "
                "utility room and wet the drywall."
            ),
            "property_context": (
                "Residential property; caller is owner."
            ),
            "timing_summary": (
                "Started this morning; source is off."
            ),
            "safety_summary": (
                "No sewage, electrical contact, or "
                "structural danger reported."
            ),
            "service_requested": (
                "water damage restoration"
            ),
            "service_key": (
                "water_damage_restoration"
            ),
            "service_status": "supported",
        },
        call_id="call-continuation-2",
        caller_number="+12315550102",
    )
    assert details["ready_to_submit"] is False
    assert details["next_question"] == (
        "What is your full name?"
    )
    assert details["safe_message"].endswith("?")

    named = await service.execute(
        "capture_intake_progress",
        {"name": "Josh Aldrich"},
        call_id="call-continuation-2",
        caller_number="+12315550102",
    )
    assert "property address" in (
        named["next_question"].lower()
    )

    addressed = await service.execute(
        "capture_intake_progress",
        {
            "address": (
                "8805 East Melendy Street, "
                "Ludington, MI 49431"
            )
        },
        call_id="call-continuation-2",
        caller_number="+12315550102",
    )
    assert "email address" in (
        addressed["next_question"].lower()
    )

    complete = await service.execute(
        "capture_intake_progress",
        {"email_status": "declined"},
        call_id="call-continuation-2",
        caller_number="+12315550102",
    )
    assert complete["ready_to_submit"] is True
    assert complete["continuation_required"] is False
    assert complete["next_question"] == ""
    db.close()


def test_agent_and_tools_forbid_terminal_classification(
    project_root,
) -> None:
    agent = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    assert (
        "A classification result is never the end "
        "of the intake."
        in agent
    )
    assert "immediately ask next_question" in agent

    import yaml

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
    assert "Never leave the caller in silence" in tools[
        "floodman_capture_intake_progress"
    ]["description"]
