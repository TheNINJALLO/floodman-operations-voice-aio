from __future__ import annotations

import asyncio
import importlib
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.briefings import (
    BriefingError,
    DECKARD_MODEL,
    OllamaBriefingClient,
    PostCallBriefings,
    call_digest,
    private_ollama_url,
    source_for,
    valid_model,
)
from app.config import Settings
from app.db import Database
from app.models import IntakeState


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POST_CALL_BRIEFINGS_ENABLED", "true")
    monkeypatch.setenv("BRIEFING_BACKEND", "ollama")
    monkeypatch.setenv("BRIEFING_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.delenv("BRIEFING_MODEL", raising=False)
    monkeypatch.delenv("BRIEFING_THREADS", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ADMIN_TOKEN", "test-recovery-secret")
    return Settings.from_env()


@pytest.fixture
def database(settings):
    return Database(settings.database_path)


def ended_call(database, name="Original caller", call_uuid="briefing-call"):
    state = IntakeState(
        call_uuid=call_uuid, name=name, phone="+12315550100",
        address="1 Main Street", description="Water in the basement.",
        urgency="normal", department="estimating", completed=True,
    )
    call_id = database.create_call(state)
    database.add_message(call_id, "assistant", "How can I help?")
    database.add_message(call_id, "caller", "Water in the basement.")
    database.finish_call(call_id, "completed")
    return database.get_call(call_id)


def draft():
    return {"summary": "The caller reported water in the basement.",
            "evidence": [{"message_index": 2, "quote": "Water in the basement."}]}


def client_for(settings, body=None, *, tags=None, requests=None):
    response_body = body if body is not None else {
        "done": True, "done_reason": "stop", "message": {"content": json.dumps(draft())},
    }
    tags_body = tags if tags is not None else {"models": [{"name": DECKARD_MODEL}]}

    def respond(request):
        if requests is not None:
            requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=tags_body)
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json=response_body)

    return OllamaBriefingClient(settings, transport=httpx.MockTransport(respond))


def due_now(database, call_id):
    with database.connect() as connection:
        connection.execute("UPDATE call_briefings SET next_attempt_at=0 WHERE call_id=?", (call_id,))


def test_deckard_and_cpu_request_defaults_match_next_project(settings):
    assert DECKARD_MODEL == "floodman-deckard:40b-q4_k_m"
    assert settings.briefing_model == DECKARD_MODEL
    assert settings.briefing_threads == 4
    assert settings.briefing_timeout_seconds == 120


@pytest.mark.parametrize("address", [
    "http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434",
    "http://10.1.2.3:11434", "https://172.16.0.2:11434", "http://192.168.1.5:11434",
    "http://[fd00::12]:11434", "http://ollama:11434", "http://host.docker.internal:11434",
])
def test_private_ollama_origins_are_accepted(address):
    assert private_ollama_url(address) == address.rstrip("/")


@pytest.mark.parametrize("address", [
    "https://ollama.com", "http://8.8.8.8:11434", "http://169.254.169.254",
    "http://100.64.0.1:11434", "http://192.0.2.1:11434", "http://[2001:4860:4860::8888]",
    "http://localhost.attacker.example:11434", "http://user:password@localhost:11434",
    "http://127.0.0.1:11434/api", "http://127.0.0.1:11434?key=secret",
    "http://127.0.0.1:11434#fragment", "file:///tmp/model", "http://127.0.0.1:0",
    "http://127.0.0.1:99999", "http://[bad-ip]", "",
])
def test_public_or_credentialed_ollama_addresses_are_rejected(address):
    with pytest.raises(ValueError):
        private_ollama_url(address)


@pytest.mark.parametrize("model", ["qwen3:cloud", "floodman-cloud:40b", "", "name with spaces", "x\nsecret"])
def test_cloud_or_malformed_model_names_are_rejected(model):
    with pytest.raises(ValueError):
        valid_model(model)


@pytest.mark.parametrize("raw,expected", [("-1", 1), ("1000", 16), ("invalid", 4)])
def test_briefing_cpu_threads_are_bounded(settings, monkeypatch, raw, expected):
    monkeypatch.setenv("BRIEFING_THREADS", raw)
    assert Settings.from_env().briefing_threads == expected


@pytest.mark.asyncio
async def test_native_ollama_chat_uses_schema_cpu_budget_and_installed_model(settings, database):
    call = ended_call(database)
    requests = []
    result = await client_for(settings, requests=requests).generate(source_for(call))
    assert result == draft()
    assert [(request.method, request.url.path) for request in requests] == [("GET", "/api/tags"), ("POST", "/api/chat")]
    payload = json.loads(requests[-1].content)
    assert payload["model"] == DECKARD_MODEL
    assert payload["stream"] is False and payload["think"] is False
    assert payload["keep_alive"] == "5m"
    assert payload["options"] == {"temperature": 0, "num_gpu": 0, "num_thread": 4, "num_ctx": 8192, "num_predict": 700}
    assert payload["format"]["additionalProperties"] is False
    assert set(payload["format"]["required"]) == {"summary", "evidence"}
    assert json.loads(payload["messages"][1]["content"])["saved_intake"]["department"] == "estimating"


@pytest.mark.asyncio
@pytest.mark.parametrize("tags", [{"models": []}, {"models": None}, {"models": 123}, [], {"models": [None, "bad"]}])
async def test_missing_or_malformed_model_catalog_fails_health_cleanly(settings, tags):
    assert await client_for(settings, tags=tags).available() is False


@pytest.mark.asyncio
async def test_unavailable_model_does_not_send_chat(settings, database):
    requests = []
    with pytest.raises(BriefingError) as error:
        await client_for(settings, tags={"models": [{"name": "other-local-model"}]}, requests=requests).generate(source_for(ended_call(database)))
    assert error.value.code == "model_unavailable"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_redirected_model_catalog_is_not_followed(settings):
    requested = []
    def redirect(request):
        requested.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://public.example/api/tags"})
    client = OllamaBriefingClient(settings, transport=httpx.MockTransport(redirect))
    assert await client.available() is False
    assert requested == ["http://127.0.0.1:11434/api/tags"]


@pytest.mark.asyncio
async def test_native_request_timeout_has_a_safe_error_code(settings, database):
    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": DECKARD_MODEL}]})
        raise httpx.ReadTimeout("private remote request details", request=request)
    client = OllamaBriefingClient(settings, transport=httpx.MockTransport(respond))
    with pytest.raises(BriefingError) as error:
        await client.generate(source_for(ended_call(database)))
    assert error.value.code == "timeout" and "private" not in str(error.value)


@pytest.mark.asyncio
async def test_empty_transcript_never_contacts_ollama(settings, database):
    requests = []
    client = client_for(settings, requests=requests)
    with pytest.raises(BriefingError) as error:
        await client.generate({"messages": [], "saved_intake": {}})
    assert error.value.code == "no_transcript" and requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    ({"done": False, "message": {"content": json.dumps(draft())}}, "incomplete_response"),
    ({"done": True, "done_reason": "length", "message": {"content": json.dumps(draft())}}, "incomplete_response"),
    ({"done": True, "message": {"content": "not JSON"}}, "invalid_response"),
    ({"done": True, "message": {"content": json.dumps(draft() | {"recipients": ["attacker"]})}}, "invalid_response"),
    ({"done": True, "message": {"content": json.dumps({"summary": "No evidence.", "evidence": []})}}, "invalid_response"),
    ({"done": True, "message": {"content": json.dumps({"summary": "Invented.", "evidence": [{"message_index": 200, "quote": "Water in the basement."}]})}}, "invalid_evidence"),
    ({"done": True, "message": {"content": json.dumps({"summary": "Invented.", "evidence": [{"message_index": 2, "quote": "Payment is verified"}]})}}, "invalid_evidence"),
])
async def test_invalid_or_ungrounded_responses_are_rejected(settings, database, body, code):
    with pytest.raises(BriefingError) as error:
        await client_for(settings, body).generate(source_for(ended_call(database)))
    assert error.value.code == code


def test_bounded_source_keeps_original_message_numbers_and_excludes_internal_metadata(database):
    call = ended_call(database)
    call["snapshot"]["metadata"] = {"secret": "do-not-copy"}
    call["messages"] = [{"role": "caller", "text": f"Message {index}: " + "x" * 490} for index in range(60)]
    source = source_for(call)
    assert source["transcript_truncated"] is True
    assert len(source["messages"]) <= 40
    assert sum(len(message["text"]) for message in source["messages"]) <= 10000
    assert source["messages"][-1]["message_index"] == 60
    assert all(call["messages"][message["message_index"] - 1]["text"] == message["text"] for message in source["messages"])
    assert "do-not-copy" not in json.dumps(source)


def test_briefing_source_excludes_assistant_readbacks_and_internal_identifiers(database):
    call = ended_call(database)
    call["snapshot"]["call_uuid"] = "internal-reference-not-needed-for-a-draft"
    call["messages"].append({"role": "assistant", "text": "Repeated assistant-only words"})
    source = source_for(call)
    assert source["messages"] == [{"message_index": 2, "role": "caller", "text": "Water in the basement."}]
    assert source["saved_intake"]["department"] == "estimating"
    assert "internal-reference-not-needed-for-a-draft" not in json.dumps(source)
    assert "Repeated assistant-only words" not in json.dumps(source)
    assert source["transcript_truncated"] is False


@pytest.mark.asyncio
async def test_disabled_briefings_never_connect_or_enqueue(settings, database):
    settings.post_call_briefings_enabled = False
    call = ended_call(database)
    async def forbidden(_source):
        raise AssertionError("Disabled briefings must not perform inference")
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=forbidden))
    assert service.queue(call["id"]) is False
    await service.start()
    assert service.task is None
    assert await service.process_one() is False
    assert database.get_call_briefing(call["id"]) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_invalid_optional_configuration_does_not_break_the_voice_runtime(settings, database, enabled):
    settings.post_call_briefings_enabled = enabled
    settings.briefing_ollama_url = "https://public.example"
    service = PostCallBriefings(settings, database, lambda: 0)
    assert service.configuration_error is True and service.enabled is False
    assert service.queue(1) is False


@pytest.mark.asyncio
async def test_finished_call_draft_preserves_authoritative_intake_and_recipients(settings, database):
    call = ended_call(database)
    database.record_notification(call["id"], "completed_intake", "staff@example.test", "sent", "accepted", "immutable-delivery", channel="email")
    before = deepcopy(database.get_call(call["id"]))
    service = PostCallBriefings(settings, database, lambda: 0, client=client_for(settings))
    assert service.queue(call["id"])
    assert not service.queue(call["id"])
    assert await service.process_one()
    assert database.get_call_briefing(call["id"])["status"] == "ready"
    assert database.get_call_briefing(call["id"])["result"] == draft()
    assert not service.queue(call["id"])
    assert database.get_call(call["id"]) == before


@pytest.mark.asyncio
async def test_durable_queue_recovers_after_restart_and_explicit_regeneration(settings, database):
    call = ended_call(database)
    service = PostCallBriefings(settings, database, lambda: 0, client=client_for(settings))
    assert service.queue(call["id"])
    claimed = database.claim_call_briefing()
    assert claimed["status"] == "running"
    reopened = Database(settings.database_path)
    assert reopened.get_call_briefing(call["id"])["status"] == "pending"
    restarted = PostCallBriefings(settings, reopened, lambda: 0, client=client_for(settings))
    assert await restarted.process_one()
    assert reopened.get_call_briefing(call["id"])["status"] == "ready"
    old_generation = reopened.get_call_briefing(call["id"])["generation"]
    assert restarted.queue(call["id"], force=True)
    assert reopened.get_call_briefing(call["id"])["generation"] == old_generation + 1
    assert reopened.finish_call_briefing(claimed, result={"summary": "stale"}) is False


@pytest.mark.asyncio
async def test_bounded_retries_store_codes_and_never_raw_provider_errors(settings, database, caplog):
    call = ended_call(database)
    async def broken(_source):
        raise RuntimeError("PRIVATE TOKEN AND CUSTOMER CONTENT")
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=broken))
    service.queue(call["id"])
    for attempt in range(1, 4):
        due_now(database, call["id"])
        assert await service.process_one()
        job = database.get_call_briefing(call["id"])
        assert job["attempts"] == attempt
        assert job["status"] == ("failed" if attempt == 3 else "pending")
        assert job["error_code"] == "generation_failed"
        assert "PRIVATE TOKEN" not in json.dumps(job)
    assert "PRIVATE TOKEN" not in caplog.text
    assert not await service.process_one()


@pytest.mark.asyncio
async def test_active_calls_defer_inference_and_new_call_cancels_pending_work(settings, database):
    call = ended_call(database)
    active = [1]
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def delayed(_source):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    service = PostCallBriefings(settings, database, lambda: active[0], client=SimpleNamespace(generate=delayed))
    service.queue(call["id"])
    assert await service.process_one() is False
    assert database.get_call_briefing(call["id"])["attempts"] == 0
    active[0] = 0
    work = asyncio.create_task(service.process_one())
    await asyncio.wait_for(entered.wait(), timeout=1)
    active[0] = 1
    assert await asyncio.wait_for(work, timeout=1)
    assert cancelled.is_set()
    job = database.get_call_briefing(call["id"])
    assert job["status"] == "pending" and job["error_code"] == "live_call" and job["attempts"] == 0


@pytest.mark.asyncio
async def test_cancelled_worker_returns_job_to_durable_pending_queue(settings, database):
    call = ended_call(database)
    entered = asyncio.Event()
    async def delayed(_source):
        entered.set()
        await asyncio.Event().wait()
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=delayed))
    service.queue(call["id"])
    work = asyncio.create_task(service.process_one())
    await asyncio.wait_for(entered.wait(), timeout=1)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    job = database.get_call_briefing(call["id"])
    assert job["status"] == "pending" and job["error_code"] == "restart"


@pytest.mark.asyncio
async def test_staff_correction_hides_old_draft_and_requeues_changed_source(settings, database):
    call = ended_call(database)
    service = PostCallBriefings(settings, database, lambda: 0, client=client_for(settings))
    service.queue(call["id"])
    await service.process_one()
    database.update_call(call["id"], {"name": "Staff corrected name"})
    corrected = database.get_call(call["id"])
    view = service.view(corrected)
    assert view["stale"] and view["job"]["result"] == {}
    assert database.get_call_briefing(call["id"])["result"] == draft()
    service.queue(call["id"])
    assert database.get_call_briefing(call["id"])["source_digest"] == call_digest(corrected)


@pytest.mark.asyncio
async def test_correction_during_inference_rejects_stale_result(settings, database):
    call = ended_call(database)
    async def correcting(_source):
        database.update_call(call["id"], {"description": "Staff corrected scope"})
        return draft()
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=correcting))
    service.queue(call["id"])
    await service.process_one()
    job = database.get_call_briefing(call["id"])
    assert job["status"] == "pending" and job["result"] == {}
    assert job["source_digest"] == call_digest(database.get_call(call["id"]))


@pytest.mark.asyncio
async def test_pending_source_change_and_model_change_are_requeued_before_inference(settings, database):
    call = ended_call(database)
    async def forbidden(_source):
        raise AssertionError("A stale pending job must be refreshed before inference")
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=forbidden))
    service.queue(call["id"])
    database.update_call(call["id"], {"address": "2 Corrected Street"})
    assert await service.process_one()
    refreshed = database.get_call_briefing(call["id"])
    assert refreshed["status"] == "pending" and refreshed["attempts"] == 0
    assert refreshed["source_digest"] == call_digest(database.get_call(call["id"]))
    settings.briefing_model = "qwen3:4b"
    assert await service.process_one()
    assert database.get_call_briefing(call["id"])["model"] == "qwen3:4b"


@pytest.mark.asyncio
async def test_legacy_pending_job_cannot_summarize_an_active_call(settings, database):
    call = ended_call(database)
    async def forbidden(_source):
        raise AssertionError("An active call must not start inference")
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=forbidden))
    service.queue(call["id"])
    database.update_call(call["id"], {"status": "active"})
    assert await service.process_one()
    job = database.get_call_briefing(call["id"])
    assert job["status"] == "pending" and job["error_code"] == "live_call"


@pytest.mark.asyncio
async def test_deleted_call_removes_pending_and_running_briefings(settings, database):
    call = ended_call(database)
    async def deleting(_source):
        database.delete_call(call["id"])
        return draft()
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace(generate=deleting))
    service.queue(call["id"])
    await service.process_one()
    assert database.get_call_briefing(call["id"]) is None
    other = ended_call(database, call_uuid="delete-pending")
    service.queue(other["id"])
    database.delete_call(other["id"])
    assert database.get_call_briefing(other["id"]) is None


@pytest.fixture
def portal(settings):
    previous = sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    database = module.runtime.database
    manager = database.create_user("briefing.manager", "Manager", "unused", "manager", {}, None)
    token, csrf = database.create_session(manager, False, 1)
    call = ended_call(database)
    client = TestClient(module.app, base_url="https://testserver")
    client.cookies.set("floodman_session", token)
    yield module, client, call, csrf
    sys.modules.pop("app.main", None)
    if previous is not None:
        sys.modules["app.main"] = previous


def test_briefing_route_requires_manager_authentication_csrf_and_ended_call(portal):
    module, client, call, csrf = portal
    path = f"/calls/{call['id']}/briefing"
    assert client.post(path, data={"csrf_token": "invalid"}).status_code == 403
    assert client.post(path, data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    assert module.runtime.database.get_call_briefing(call["id"])["status"] == "pending"
    active_id = module.runtime.database.create_call(IntakeState(call_uuid="still-active"))
    response = client.post(f"/calls/{active_id}/briefing", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert client.post("/calls/999999/briefing", data={"csrf_token": csrf}).status_code == 404
    viewer = module.runtime.database.create_user("briefing.viewer", "Viewer", "unused", "viewer", {}, None)
    token, viewer_csrf = module.runtime.database.create_session(viewer, False, 1)
    client.cookies.set("floodman_session", token)
    assert client.post(path, data={"csrf_token": viewer_csrf}).status_code == 403
    client.cookies.clear()
    assert client.post(path, data={"csrf_token": csrf}).status_code == 401
