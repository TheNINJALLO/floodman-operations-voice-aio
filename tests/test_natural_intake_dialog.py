from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
from app.intake_flow import (
    contact_collection_question,
    contact_confirmation_question,
    next_intake_state,
)
from app.roomflow.client import RoomflowClient


DETAILS = {
    "description": "A water heater leaked into the utility room.",
    "service_requested": "water damage restoration",
    "property_context": "Residential home; caller owns it.",
    "timing_summary": "It started this morning.",
    "safety_summary": "No electrical, sewage, or other safety concern.",
}


def test_contact_prompts_are_short_and_natural() -> None:
    assert contact_collection_question("name") == (
        "What name should I put this under?"
    )
    assert contact_collection_question("email") == (
        "What's the best email for you? You can say skip."
    )
    assert contact_collection_question("phone") == (
        "What's the best callback number?"
    )
    assert contact_collection_question("address") == (
        "What's the full service address?"
    )

    snapshot = {
        "name": "Josh Aldrich",
        "email": "josh@example.com",
        "phone": "+12318840943",
        "caller_number": "+12318840943",
        "address": "8805 East Melendy Street, Ludington, MI 49431",
    }
    assert contact_confirmation_question(
        snapshot, "name"
    ) == "Josh Aldrich, right?"
    assert contact_confirmation_question(
        snapshot, "email"
    ) == "josh at example dot com, right?"
    assert contact_confirmation_question(
        snapshot, "phone"
    ) == "Is this the best number to call you back on?"
    assert contact_confirmation_question(
        snapshot, "address"
    ) == (
        "8805 East Melendy Street, Ludington, MI 49431, right?"
    )


def test_detail_prompts_are_one_clean_question() -> None:
    snapshots = (
        ({}, "What happened, and where?"),
        (
            {
                "description": "Water in the basement.",
                "service_status": "supported",
            },
            "Is this a home or a business?",
        ),
    )
    for snapshot, expected in snapshots:
        state = next_intake_state(snapshot)
        assert state["safe_message"] == expected
        assert state["safe_message"].count("?") == 1


async def test_complete_call_uses_only_direct_questions(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    call_id = "natural-intake-call"
    phone = "+12318840943"

    first = await service.execute(
        "capture_intake_progress",
        DETAILS,
        call_id=call_id,
        caller_number=phone,
    )
    assert first["safe_message"] == (
        "What name should I put this under?"
    )

    named = await service.execute(
        "capture_intake_progress",
        {"name": "Josh Aldrich"},
        call_id=call_id,
        caller_number=phone,
    )
    assert named["safe_message"] == "Josh Aldrich, right?"

    name_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "name",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=phone,
    )
    assert name_ok["safe_message"] == (
        "What's the best email for you? You can say skip."
    )

    emailed = await service.execute(
        "capture_intake_progress",
        {"email": "josh@example.com"},
        call_id=call_id,
        caller_number=phone,
    )
    assert emailed["safe_message"] == (
        "josh at example dot com, right?"
    )

    email_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "email",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=phone,
    )
    assert email_ok["safe_message"] == (
        "Is this the best number to call you back on?"
    )

    phone_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "phone",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=phone,
    )
    assert phone_ok["safe_message"] == (
        "What's the full service address?"
    )

    addressed = await service.execute(
        "capture_intake_progress",
        {
            "address": (
                "8805 East Melendy Street, "
                "Ludington, MI 49431"
            ),
        },
        call_id=call_id,
        caller_number=phone,
    )
    assert addressed["safe_message"].endswith(", right?")

    complete = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "address",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=phone,
    )
    assert complete["submitted"] is True
    assert complete["end_call_after_message"] is True
    assert complete["safe_message"].startswith("You're all set.")
    assert "within 24 hours" in complete["safe_message"]
    assert "recorded" not in complete["safe_message"].lower()
    db.close()


def test_voice_policy_and_tool_are_direct(
    project_root,
) -> None:
    import yaml

    agent = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    assert "respond with the capture tool call only" in agent.lower()
    assert "do not speak before the tool call" in agent.lower()
    assert "the engine speaks safe_message directly" in agent.lower()

    config = yaml.safe_load(
        (
            project_root
            / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    description = config["in_call_tools"][
        "floodman_capture_intake_progress"
    ]["description"].lower()
    assert "tool-only" in description
    assert "engine speaks" in description


def test_turn_detection_favors_complete_contact_details(
    project_root,
) -> None:
    import importlib.util
    import sys

    path = (
        project_root
        / "scripts/apply_production_hybrid_ai.py"
    )
    spec = importlib.util.spec_from_file_location(
        "natural_dialog_profile",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    patch = module.production_patch()
    stt = patch["pipelines"][
        "floodman_production"
    ]["options"]["stt"]
    assert stt["eot_threshold"] == (
        "${DEEPGRAM_EOT_THRESHOLD:-0.82}"
    )
    assert stt["eager_eot_threshold"] == (
        "${DEEPGRAM_EAGER_EOT_THRESHOLD:-0.70}"
    )
    assert stt["eot_timeout_ms"] == (
        "${DEEPGRAM_EOT_TIMEOUT_MS:-1500}"
    )
    assert patch["barge_in"][
        "post_tts_end_protection_ms"
    ] == (
        "${FLOODMAN_POST_TTS_END_PROTECTION_MS:-120}"
    )
