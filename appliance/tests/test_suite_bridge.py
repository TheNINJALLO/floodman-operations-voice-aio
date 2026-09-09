from __future__ import annotations

import base64
import hashlib
import hmac

from app.config import Settings
from app.db import Database
from app.models import IntakeState
from app.suite_bridge import BusinessSuiteBridge, signed_headers


def configured_bridge(monkeypatch, tmp_path) -> tuple[Database, BusinessSuiteBridge]:
    secret = base64.b64encode(b"b" * 32).decode("ascii")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BUSINESS_SUITE_EVENTS_ENABLED", "true")
    monkeypatch.setenv(
        "BUSINESS_SUITE_EVENTS_URL",
        "http://127.0.0.1:9004/webhooks/ai-calling/deterministic",
    )
    monkeypatch.setenv("AI_CALLING_HMAC_KEYS", f"call-v1:{secret}")
    monkeypatch.setenv("BUSINESS_SUITE_ORGANIZATION_ID", "floodman")
    monkeypatch.setenv("BUSINESS_SUITE_WORKSPACE_ID", "test-workspace")
    settings = Settings.from_env()
    database = Database(settings.database_path)
    return database, BusinessSuiteBridge(settings, database)


def test_bridge_commits_ordered_canonical_events_before_delivery(monkeypatch, tmp_path):
    database, bridge = configured_bridge(monkeypatch, tmp_path)
    state = IntakeState(
        call_uuid="provider-call-1",
        caller_number="+13135550123",
        phone="+13135550123",
        name="Jamie Rivera",
        email="jamie@example.com",
        address="10 Main Street, Detroit, MI 48201",
        property_context="home",
        description="Water is coming through the basement wall",
        service_key="water_damage_restoration",
        service_status="supported",
        completed=True,
    )
    call_id = database.create_call(state)

    bridge.queue(call_id, state, "call-started")
    bridge.queue(call_id, state, "transcript-updated")
    bridge.queue(call_id, state, "call-ended")

    events = []
    for expected_sequence in range(3):
        item = database.claim_business_event(now=1)
        assert item is not None
        assert item["event_sequence"] == expected_sequence
        assert item["payload"]["event_sequence"] == expected_sequence
        assert item["payload"]["provider_call_id"] == state.call_uuid
        assert "transcript" not in item["payload"]
        events.append(item["payload"])
        database.complete_business_event(item["id"])

    assert events[0]["event_type"] == "call-started"
    assert events[0]["transcript_available"] is False
    assert events[-1]["event_type"] == "call-ended"
    assert events[-1]["transcript_available"] is True
    assert database.business_event_counts() == {"pending": 0, "sending": 0, "sent": 3}


def test_bridge_marks_partial_terminal_calls_for_review(monkeypatch, tmp_path):
    database, bridge = configured_bridge(monkeypatch, tmp_path)
    state = IntakeState(call_uuid="partial-1", description="There is water downstairs")
    call_id = database.create_call(state)

    bridge.queue(call_id, state, "call-ended")

    item = database.claim_business_event(now=1)
    assert item is not None
    reasons = item["payload"]["review_reasons"]
    assert "CALLER_NAME_MISSING" in reasons
    assert "CALLBACK_PHONE_MISSING" in reasons
    assert "SERVICE_ADDRESS_MISSING" in reasons
    assert "PARTIAL_CALL" in reasons


def test_business_suite_signature_matches_contract():
    secret = b"s" * 32
    body = b'{"event":"call-started"}'
    headers = signed_headers("call-v1", secret, body, 1_700_000_000)
    expected = hmac.new(secret, b"1700000000." + body, hashlib.sha256).digest()

    assert headers["x-floodman-key-id"] == "call-v1"
    assert headers["x-floodman-timestamp"] == "1700000000"
    assert base64.b64decode(headers["x-floodman-signature"]) == expected

