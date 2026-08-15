from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import yaml

from app.business import BusinessOperations
from app.db import Database
from app.intake import classify_service_request
from app.notifications import build_intake_sms
from app.roomflow.client import RoomflowClient


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_service_classification_uses_approved_catalog(settings) -> None:
    supported = classify_service_request(
        settings.service_information,
        "basement waterproofing",
        "Water comes through the basement wall after rain",
    )
    assert supported["supported"] is True
    assert supported["service_key"] == "basement_waterproofing"
    assert supported["intake_questions"]

    unsupported = classify_service_request(
        settings.service_information,
        "roof repair",
        "Several shingles are missing",
    )
    assert unsupported["supported"] is False
    assert unsupported["service_status"] == "unsupported"
    assert "does not currently offer" in unsupported["safe_message"]


def test_water_cleanup_is_not_rejected_as_plumbing(settings) -> None:
    result = classify_service_request(
        settings.service_information,
        "water damage restoration",
        "A pipe burst and flooded two rooms",
    )
    assert result["service_status"] == "supported"
    assert result["service_key"] == "water_damage_restoration"


async def test_detailed_snapshot_requires_email_or_explicit_disposition(settings) -> None:
    settings.team_sms_enabled = True
    settings.team_alert_numbers = ("+12315550001",)
    db = Database(settings.database_path)
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))

    classification = await service.execute(
        "classify_service",
        {
            "requested_service": "roof repair",
            "description": "Wind removed shingles and water is entering the attic",
        },
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )
    assert classification["service_status"] == "unsupported"

    await service.execute(
        "capture_intake_progress",
        {
            "name": "Josh Aldrich",
            "phone": "+12315550199",
            "address": "8805 East Melendy Street, Ludington, MI 49431",
            "service_requested": "roof repair",
            "service_status": "unsupported",
            "service_reason": "known_out_of_scope:roofing",
            "description": (
                "Wind removed shingles and rain is entering the attic near the east bedroom."
            ),
            "property_context": "Residential property; caller is the owner.",
            "safety_summary": "No electrical contact or structural collapse reported.",
            "timing_summary": "Started this morning and is still active during rain.",
            "insurance_summary": "No claim filed yet.",
            "evidence_summary": "Caller has photos.",
            "urgency": "urgent",
            "department": "estimating",
        },
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )

    missing_email = await service.execute(
        "submit_intake",
        {},
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )
    assert missing_email["ok"] is False
    assert any("email" in item for item in missing_email["missing"])

    await service.execute(
        "capture_intake_progress",
        {"email_status": "declined"},
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )

    for field in ("name", "email", "phone", "address"):
        await service.execute(
            "capture_intake_progress",
            {
                "confirm_field": field,
                "confirmation": "yes",
            },
            call_id="call-unsupported-1",
            caller_number="+12315550199",
        )
    submitted = await service.execute(
        "submit_intake",
        {},
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )
    assert submitted["ok"] is True
    assert submitted["service_status"] == "unsupported"
    assert "does not currently offer roof repair" in submitted["safe_message"]
    assert "within 24 hours" in submitted["safe_message"]

    repeated = await service.execute(
        "submit_intake",
        {},
        call_id="call-unsupported-1",
        caller_number="+12315550199",
    )
    assert repeated["ok"] is True
    assert repeated["deduplicated"] is True

    intake = db.get_call_intake("call-unsupported-1")
    assert intake is not None
    assert intake["status"] == "unsupported"
    assert intake["email_status"] == "declined"
    alerts = [
        item
        for item in db.list_outbox()
        if item["operation"] == "team_sms_alert"
    ]
    assert len(alerts) == 1
    body = alerts[0]["payload"]["body"]
    assert "OUT-OF-SCOPE" in body
    assert "Josh Aldrich" in body
    assert "Email: declined" in body
    assert "Wind removed shingles" in body
    db.close()


async def test_hangup_finalization_sends_all_recovered_information(settings) -> None:
    settings.team_sms_enabled = True
    settings.team_alert_numbers = ("+12315550002",)
    db = Database(settings.database_path)
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))

    await service.execute(
        "capture_intake_progress",
        {
            "name": "Partial Caller",
            "email": "partial@example.com",
            "address": "42 River Road, Traverse City, MI 49684",
            "service_requested": "water damage restoration",
            "service_status": "supported",
            "service_key": "water_damage_restoration",
            "description": (
                "A water heater failed and water reached the utility room and hallway."
            ),
            "safety_summary": (
                "Source is shut off; no sewage; water is near but not touching the panel."
            ),
            "timing_summary": "Started about 30 minutes ago.",
            "urgency": "emergency",
            "department": "emergency",
        },
        call_id="call-hangup-1",
        caller_number="+12315550222",
    )
    finalized = await service.execute(
        "finalize_inbound_intake",
        {
            "transcript": [
                {"role": "assistant", "content": "Tell me what happened."},
                {
                    "role": "user",
                    "content": (
                        "My water heater failed and water is spreading into the hallway."
                    ),
                },
            ],
            "summary": "Caller disconnected while reporting active water damage.",
            "outcome": "caller_hangup",
            "metadata": {"context": "floodman_inbound"},
        },
        call_id="call-hangup-1",
        caller_number="+12315550222",
    )
    assert finalized["ok"] is True
    intake = db.get_call_intake("call-hangup-1")
    assert intake is not None
    assert intake["status"] == "partial_hangup"
    assert "Caller:" in intake["transcript_text"]
    assert intake["callback_id"]
    alerts = [
        item
        for item in db.list_outbox()
        if item["operation"] == "team_sms_alert"
    ]
    assert len(alerts) == 1
    body = alerts[0]["payload"]["body"]
    assert "PARTIAL CALL RECOVERY" in body
    assert "partial@example.com" in body
    assert "water heater failed" in body.lower()
    assert "Full transcript" in body
    db.close()


async def test_immediate_hangup_with_caller_id_still_alerts_team(settings) -> None:
    settings.team_sms_enabled = True
    settings.team_alert_numbers = ("+12315550003",)
    db = Database(settings.database_path)
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))

    finalized = await service.execute(
        "finalize_inbound_intake",
        {
            "transcript": [],
            "summary": "",
            "outcome": "caller_hangup",
            "metadata": {"context": "floodman_inbound"},
        },
        call_id="call-immediate-hangup",
        caller_number="+12315550999",
    )
    assert finalized["ok"] is True
    intake = db.get_call_intake("call-immediate-hangup")
    assert intake is not None
    assert intake["status"] == "partial_hangup"
    assert intake["phone"] == "+12315550999"
    assert intake["callback_id"]
    alerts = [
        item
        for item in db.list_outbox()
        if item["operation"] == "team_sms_alert"
    ]
    assert len(alerts) == 1
    body = alerts[0]["payload"]["body"]
    assert "+12315550999" in body
    assert "Call ID: call-immediate-hangup" in body
    db.close()


def test_call_intake_api_exposes_full_transcript(settings) -> None:
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        db = client.app.state.database
        db.upsert_call_intake(
            "call-web-1",
            {
                "name": "Web Caller",
                "phone": "+12315550333",
                "email": "web@example.com",
                "address": "10 Lake Street, Ludington, MI",
                "service_requested": "mold remediation",
                "service_status": "supported",
                "description": "Musty odor and visible spotting in the basement.",
                "status": "complete",
                "transcript": [
                    {
                        "role": "user",
                        "content": "There is a musty smell in the basement.",
                    },
                    {
                        "role": "assistant",
                        "content": "When did you first notice it?",
                    },
                ],
                "transcript_text": (
                    "Caller: There is a musty smell in the basement.\n"
                    "Ava: When did you first notice it?"
                ),
            },
        )
        headers = {"Authorization": "Bearer admin-test-token"}
        listing = client.get("/api/v1/call-intakes", headers=headers)
        assert listing.status_code == 200
        assert any(
            item["call_id"] == "call-web-1"
            for item in listing.json()["intakes"]
        )
        detail = client.get(
            "/api/v1/call-intakes/call-web-1",
            headers=headers,
        )
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["intake"]["email"] == "web@example.com"
        assert "Caller:" in payload["intake"]["transcript_text"]


def test_voice_web_app_has_intake_history_and_transcript_dialog(project_root) -> None:
    html = (project_root / "web/index.html").read_text(encoding="utf-8")
    javascript = (project_root / "web/app.js").read_text(encoding="utf-8")
    assert "Customer calls and recovered intake" in html
    assert "callDetailDialog" in html
    assert "/api/v1/call-intakes?limit=200" in javascript
    assert "/api/v1/call-intakes/" in javascript
    assert "Full transcript" in html


def test_inbound_agent_restores_detailed_interview_and_progress_saves(project_root) -> None:
    source = (project_root / "app/ava/agents.py").read_text(encoding="utf-8")
    inbound = source.split('slug="floodman_inbound"', 1)[1].split(
        'slug="floodman_google_business"', 1
    )[0]
    assert "floodman_classify_service" in inbound
    assert "floodman_capture_intake_progress" in inbound
    assert "full name, best callback number, email" in inbound
    assert "standing water" in inbound
    assert "insurance" in inbound
    assert "within 24 hours" in inbound
    assert "never books" in inbound
    assert "floodman_schedule_inspection" not in inbound


def test_ava_tools_include_progress_classification_and_finalize(project_root) -> None:
    config = yaml.safe_load(
        (project_root / "config/ava/ai-agent.local.yaml").read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert tools["floodman_classify_service"]["enabled"] is True
    assert tools["floodman_capture_intake_progress"]["enabled"] is True
    assert tools["floodman_submit_intake"]["enabled"] is True
    assert tools["floodman_submit_intake"]["timeout_ms"] <= 2500
    assert all(
        not bool(value.get("is_global"))
        for name, value in tools.items()
        if name.startswith("floodman_")
    )
    optional_parameters = tools["floodman_capture_intake_progress"]["parameters"]
    assert optional_parameters
    assert all(parameter.get("default") == "" for parameter in optional_parameters)
    assert tools["floodman_submit_intake"]["parameters"][0]["default"] == ""


def test_optional_parameter_patch_is_built_into_image(project_root) -> None:
    patcher = (
        project_root / "scripts/patch_ava_optional_params.py"
    ).read_text(encoding="utf-8")
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    assert "Floodman optional HTTP tool parameters patch" in patcher
    assert "parameter.get(\"default\")" in patcher
    assert "COPY scripts/patch_ava_optional_params.py" in dockerfile
    assert "patch_ava_optional_params.py --ava-root /opt/ava" in dockerfile
    assert 'grep -q "Floodman optional HTTP tool parameters patch"' in dockerfile


def test_team_sms_includes_structured_context_and_web_transcript_pointer() -> None:
    body = build_intake_sms(
        {
            "call_id": "call-sms-1",
            "status": "partial_no_input",
            "service_status": "supported",
            "name": "Test Caller",
            "phone": "+12315550444",
            "email": "test@example.com",
            "address": "55 Main Street",
            "service_requested": "basement waterproofing",
            "description": "Water enters along the north wall after rain.",
            "property_context": "Residential; owner occupied.",
            "safety_summary": "No sewage or electrical contact reported.",
            "timing_summary": "Recurring for two months.",
            "insurance_summary": "No claim.",
            "evidence_summary": "Photos available.",
            "transcript_text": (
                "Caller: Water enters along the north wall after rain."
            ),
        },
        24,
    )
    assert "test@example.com" in body
    assert "Residential" in body
    assert "No sewage" in body
    assert "Full transcript" in body
