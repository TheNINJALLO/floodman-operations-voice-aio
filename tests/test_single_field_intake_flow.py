from __future__ import annotations

from app.business import BusinessOperations
from app.db import Database
from app.intake_flow import (
    contact_confirmations,
    next_intake_state,
    update_confirmation_metadata,
)
from app.roomflow.client import RoomflowClient


DETAILS = {
    "description": "A water heater leaked into the utility room.",
    "service_requested": "water damage restoration",
    "service_key": "water_damage_restoration",
    "service_status": "supported",
    "property_context": "Residential property.",
    "timing_summary": "Started this morning.",
    "safety_summary": "No immediate safety or access concern.",
}


def test_contact_order_is_name_email_phone_address() -> None:
    snapshot = {
        **DETAILS,
        "caller_number": "+12315550100",
        "phone": "+12315550100",
        "metadata": {"contact_flow_enabled": True},
    }
    assert next_intake_state(snapshot)["field"] == "name"

    named = {**snapshot, "name": "Josh Aldrich"}
    assert next_intake_state(named)["stage"] == "confirm_contact"
    assert next_intake_state(named)["field"] == "name"

    name_meta = update_confirmation_metadata(
        snapshot,
        named,
        confirm_field="name",
        confirmation="yes",
    )
    named["metadata"] = name_meta
    assert next_intake_state(named)["field"] == "email"

    emailed = {**named, "email": "josh@example.com"}
    assert next_intake_state(emailed)["field"] == "email"

    email_meta = update_confirmation_metadata(
        named,
        emailed,
        confirm_field="email",
        confirmation="yes",
    )
    emailed["metadata"] = email_meta
    assert next_intake_state(emailed)["field"] == "phone"

    phone_meta = update_confirmation_metadata(
        emailed,
        emailed,
        confirm_field="phone",
        confirmation="yes",
    )
    emailed["metadata"] = phone_meta
    assert next_intake_state(emailed)["field"] == "address"


def test_volunteered_fields_are_saved_but_confirmed_separately() -> None:
    snapshot = {
        **DETAILS,
        "caller_number": "+12315550111",
        "phone": "+12315550111",
        "name": "Josh Aldrich",
        "email": "josh@example.com",
        "address": "8805 East Melendy Street, Ludington, MI 49431",
        "metadata": {"contact_flow_enabled": True},
    }

    state = next_intake_state(snapshot)
    assert state["stage"] == "confirm_contact"
    assert state["field"] == "name"
    assert "email" not in state["next_question"].lower()
    assert "address" not in state["next_question"].lower()


def test_rejected_value_requests_correction_not_every_field() -> None:
    before = {
        **DETAILS,
        "name": "John Aldridge",
        "caller_number": "+12315550122",
        "phone": "+12315550122",
        "metadata": {"contact_flow_enabled": True},
    }
    metadata = update_confirmation_metadata(
        before,
        before,
        confirm_field="name",
        confirmation="no",
    )
    after = {**before, "metadata": metadata}
    state = next_intake_state(after)
    assert state["stage"] == "collect_contact"
    assert state["field"] == "name"
    assert "correct name" in state["next_question"].lower()


async def test_service_confirms_each_contact_without_reasking(
    settings,
) -> None:
    db = Database(settings.database_path)
    service = BusinessOperations(
        settings,
        db,
        RoomflowClient(settings, db),
    )
    call_id = "call-single-field-1"
    caller_number = "+12315550133"

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
        {
            **DETAILS,
            "name": "Josh Aldrich",
            "email": "josh@example.com",
            "address": (
                "8805 East Melendy Street, "
                "Ludington, MI 49431"
            ),
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert details["stage"] == "confirm_contact"
    assert details["next_field"] == "name"

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
    assert name_ok["confirmation_required"] is True

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
    assert email_ok["confirmation_required"] is True

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
    assert phone_ok["confirmation_required"] is True

    address_ok = await service.execute(
        "capture_intake_progress",
        {
            "confirm_field": "address",
            "confirmation": "yes",
        },
        call_id=call_id,
        caller_number=caller_number,
    )
    assert address_ok["ready_to_submit"] is True
    assert address_ok["next_question"] == ""

    saved = db.get_call_intake(call_id)
    assert saved is not None
    assert set(contact_confirmations(saved)) == {
        "name",
        "email",
        "phone",
        "address",
    }
    db.close()


def test_prompt_and_tool_enforce_one_question(project_root) -> None:
    import yaml

    agents = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    assert "Ask exactly one question per turn" in agents
    assert "name, then email, then phone, then address" in agents
    assert "confirm_field" in agents
    assert "save every extra field" in agents

    config = yaml.safe_load(
        (
            project_root
            / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    tool = config["in_call_tools"][
        "floodman_capture_intake_progress"
    ]
    names = {
        parameter["name"]
        for parameter in tool["parameters"]
    }
    assert "confirm_field" in names
    assert "confirmation" in names
    assert "exactly one" in tool["description"].lower()


def test_latency_defaults_are_conversational(project_root) -> None:
    import importlib.util
    import sys

    path = (
        project_root
        / "scripts/apply_production_hybrid_ai.py"
    )
    spec = importlib.util.spec_from_file_location(
        "single_field_latency_profile",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    patch = module.production_patch()
    pipeline = patch["pipelines"]["floodman_production"]
    stt = pipeline["options"]["stt"]
    llm = pipeline["options"]["llm"]

    assert stt["eot_timeout_ms"] == (
        "${DEEPGRAM_EOT_TIMEOUT_MS:-1500}"
    )
    assert llm["max_tokens"] == (
        "${GROQ_LLM_MAX_TOKENS:-160}"
    )
    assert llm["timeout_sec"] == (
        "${GROQ_LLM_TIMEOUT_SECONDS:-8}"
    )
    assert patch["streaming"]["jitter_buffer_ms"] == (
        "${FLOODMAN_STREAMING_JITTER_BUFFER_MS:-80}"
    )
    assert patch["streaming"]["provider_grace_ms"] == (
        "${FLOODMAN_STREAMING_PROVIDER_GRACE_MS:-40}"
    )
