from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import IntakeState


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("FLOODMAN_EMERGENCY_NUMBER", "+12315550123")
    monkeypatch.setenv("PRIMARY_MODEL_PROFILE", "deckard")
    monkeypatch.setenv("LOCAL_VOICE_STACK", "standard")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    client = TestClient(module.app, base_url="https://testserver")
    yield module, client
    client.close()
    sys.modules.pop("app.main", None)


def sign_in(module, client, role="manager"):
    user_id = module.runtime.database.create_user(role, role.title(), "unused", role, {}, None)
    token, csrf = module.runtime.database.create_session(user_id, False, 1)
    client.cookies.set("floodman_session", token)
    return {"X-CSRF-Token": csrf}


def test_assistant_view_uses_current_configuration_without_exposing_secrets(workspace):
    module, client = workspace
    assert client.get("/assistant", follow_redirects=False).headers["location"] == "/login"
    sign_in(module, client, "viewer")
    response = client.get("/assistant")
    assert response.status_code == 200
    assert "Current configuration" in response.text
    assert "Deckard" in response.text
    assert "this is Avery" in response.text and "digital assistant" in response.text
    assert 'href="/simulator"' not in response.text
    assert 'href="/voice"' not in response.text
    assert str(module.settings.database_path) not in response.text
    assert module.settings.internal_token not in response.text
    assert client.post("/api/simulate", json={"text": ""}).status_code == 403


def test_simulator_requires_manager_and_csrf(workspace):
    module, client = workspace
    assert client.post("/api/simulate", json={"text": ""}).status_code == 401
    sign_in(module, client)
    assert client.post("/api/simulate", json={"text": ""}).status_code == 403
    assert not module.runtime.database.list_calls()


def test_simulated_emergency_and_reset_do_not_alert_sync_or_delete_calls(workspace, monkeypatch):
    module, client = workspace
    headers = sign_in(module, client)

    def forbidden(*args, **kwargs):
        pytest.fail("A simulation reached a production side effect")

    monkeypatch.setattr(module.runtime.notifier, "send", forbidden)
    monkeypatch.setattr(module.runtime.notifier, "call_started", forbidden)
    monkeypatch.setattr(module.runtime.suite_bridge, "queue", forbidden)
    monkeypatch.setattr(module.runtime.briefings, "queue", forbidden)
    live_id = module.runtime.database.create_call(IntakeState(call_uuid="real-carrier-call"))
    rejected = client.post("/api/simulate", headers=headers, json={"call_uuid": "real-carrier-call", "text": "", "reset": True})
    assert rejected.status_code == 404
    assert module.runtime.database.get_call(live_id)

    first = client.post("/api/simulate", headers=headers, json={"text": ""}).json()
    assert first["reply"] == module.runtime.core.greeting()
    second = client.post("/api/simulate", headers=headers, json={"call_uuid": first["call_uuid"], "text": "", "reset": True}).json()
    assert first["call_id"] != second["call_id"]
    assert module.runtime.database.get_call(first["call_id"])["outcome"] == "simulation_reset"
    assert first["call_uuid"] not in module.runtime.simulators
    response = client.post("/api/simulate", headers=headers, json={"call_uuid": second["call_uuid"], "text": "My basement is flooding right now and the water is rising fast."})
    assert response.status_code == 200
    assert response.json()["transfer_number"] == "+12315550123"
    assert module.runtime.database.get_call(second["call_id"])["outcome"] == "simulation_completed"
    assert second["call_uuid"] not in module.runtime.simulators
    assert not module.runtime.database.list_quality_calls()
    assert module.runtime.core.notifier is module.runtime.notifier


def test_simulator_human_request_and_unfinished_turn_keep_the_test_open(workspace):
    module, client = workspace
    headers = sign_in(module, client)
    call = client.post("/api/simulate", headers=headers, json={"text": ""}).json()
    response = client.post("/api/simulate", headers=headers, json={"call_uuid": call["call_uuid"], "text": "Can I speak to a person, please?"})
    assert response.status_code == 200
    assert "call you back" in response.json()["reply"]
    assert not response.json()["transfer_number"]
    assert not response.json()["end_call"]
    waiting = client.post("/api/simulate", headers=headers, json={"call_uuid": call["call_uuid"], "text": "It's been..."}).json()
    assert waiting["wait_for_caller"] and not waiting["reply"]
    assert call["call_uuid"] in module.runtime.simulators


def test_simulator_completion_stays_local_and_preserves_transcript(workspace, monkeypatch):
    module, client = workspace
    headers = sign_in(module, client)
    call = client.post("/api/simulate", headers=headers, json={"text": ""}).json()
    session = module.runtime.simulators[call["call_uuid"]]
    session.state.stage = "anything_else"
    session.state.name = "Test Caller"
    session.state.phone = "+12315550000"
    session.state.confirmations.update(name="Test Caller", phone="+12315550000")
    session.state.metadata["conversation"] = {"kind": "message", "asked": [], "declined": []}

    def forbidden(*args, **kwargs):
        pytest.fail("Completion queued a production notification or briefing")

    monkeypatch.setattr(module.runtime.notifier, "send", forbidden)
    monkeypatch.setattr(module.runtime.suite_bridge, "queue", forbidden)
    monkeypatch.setattr(module.runtime.briefings, "queue", forbidden)
    response = client.post("/api/simulate", headers=headers, json={"call_uuid": call["call_uuid"], "text": "No thanks"})
    assert response.status_code == 200 and response.json()["end_call"]
    assert module.runtime.database.get_call(call["call_id"])["status"] == "completed"
    assert module.runtime.database.get_call(call["call_id"])["messages"]


@pytest.mark.parametrize("phrase", ["Can I speak to a person, please?", "I'd like to talk to a person", "May I speak with a person?", "Can I talk with a person?"])
def test_person_requests_follow_callback_route(workspace, phrase):
    module, client = workspace
    headers = sign_in(module, client)
    call = client.post("/api/simulate", headers=headers, json={"text": ""}).json()
    response = client.post("/api/simulate", headers=headers, json={"call_uuid": call["call_uuid"], "text": phrase}).json()
    assert "call you back" in response["reply"]
    assert not response["transfer_number"]
