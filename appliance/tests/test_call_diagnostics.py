from __future__ import annotations

import asyncio
import importlib
import json
import struct
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.audiosocket import AudioSocketConnection, AudioSocketServer, TYPE_UUID, TYPE_AUDIO
from app.call_diagnostics import CallDiagnostics, FEEDBACK_CATEGORIES, current_trace, emit_current, diagnostic_report, summary
from app.config import Settings
from app.db import Database
from app.models import IntakeState, VoiceReply
from app.tel_agent_phone import TelAgentPhoneAgent
from app.llm import LocalLLM
from app.tts import LocalTTS


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ADMIN_TOKEN", "do-not-export-admin-secret")
    monkeypatch.setenv("SMTP_PASSWORD", "do-not-export-smtp-secret")
    monkeypatch.setenv("SMTP_USERNAME", "test-mailer")
    return Settings.from_env()


@pytest.mark.asyncio
async def test_events_are_local_persistent_isolated_and_bounded(configured):
    database = Database(configured.database_path)
    first_id = database.create_call(IntakeState(call_uuid="first"))
    second_id = database.create_call(IntakeState(call_uuid="second"))
    recorder = CallDiagnostics(database, configured)
    await recorder.start()
    first = recorder.begin(first_id)
    second = recorder.begin(second_id)
    first.turn = 1
    first.emit("first_audio", first_audio_ms=2750, level="warning", token="do-not-export")
    second.emit("barge_in", buffered_audio_bytes=3200)
    first.emit("call_ended", outcome="completed")
    await recorder.stop()

    restarted = Database(configured.database_path)
    report = restarted.get_call_diagnostics(first_id, limit=2)
    assert report["event_count"] == 3 and report["events_truncated"]
    assert report["runtime"]["code_fingerprint"]
    assert report["events"][0]["event"] == "first_audio"
    assert summary(report)["max_first_audio_ms"] == 2750
    assert summary(report)["warnings"] == 1
    assert [event["event"] for event in restarted.get_call_diagnostics(second_id)["events"]] == ["call_started", "barge_in"]
    exported = json.dumps(diagnostic_report(database.get_call(first_id), report))
    for secret in (configured.admin_token, "do-not-export", "do-not-export-smtp-secret"):
        assert secret not in exported
    assert "runtime.env" not in exported
    assert all(event["call_id"] == first_id for event in report["events"])
    assert report["events"][0]["elapsed_ms"] <= report["events"][1]["elapsed_ms"]


class Writer:
    def write(self, _data):
        pass

    async def drain(self):
        pass


@pytest.mark.asyncio
async def test_slow_diagnostic_disk_does_not_block_audio(configured, monkeypatch):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="slow-disk"))
    recorder = CallDiagnostics(database, configured)
    gate = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = database.add_call_events

    def slow_write(events):
        loop.call_soon_threadsafe(entered.set)
        gate.wait(timeout=3)
        original(events)

    monkeypatch.setattr(database, "add_call_events", slow_write)
    await recorder.start()
    trace = recorder.begin(call_id)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        connection = AudioSocketConnection(asyncio.StreamReader(), Writer(), configured)
        connection.trace = trace
        connection.begin_provider_output(since=time.perf_counter() - 3)
        await asyncio.wait_for(connection.write_provider_audio(b"\x00\x00" * 160), timeout=0.25)
        assert not gate.is_set()
    finally:
        gate.set()
        await recorder.stop()
    events = database.get_call_diagnostics(call_id)["events"]
    assert events[-1]["event"] == "first_audio" and events[-1]["level"] == "warning"


@pytest.mark.asyncio
async def test_failure_and_overflow_are_visible_without_exposing_errors(configured, monkeypatch, caplog):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="diagnostic-failure"))
    recorder = CallDiagnostics(database, configured)
    recorder.queue = asyncio.Queue(maxsize=1)
    trace = recorder.begin(call_id)
    trace.emit("barge_in")
    assert recorder.status()["discarded_events"] == 1
    original = database.add_call_events

    def broken_write(_events):
        raise OSError("private-provider-token")

    monkeypatch.setattr(database, "add_call_events", broken_write)
    await recorder.start()
    await asyncio.wait_for(recorder.queue.join(), timeout=1)
    assert recorder.status()["failed_events"] == 1
    assert "private-provider-token" not in caplog.text
    monkeypatch.setattr(database, "add_call_events", original)
    trace.emit("call_ended")
    await recorder.stop()
    assert database.get_call_diagnostics(call_id)["events"][0]["details"]["dropped_events"] == 1


@pytest.mark.asyncio
async def test_business_transaction_takes_priority_and_diagnostics_retry(configured):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="busy-database"))
    recorder = CallDiagnostics(database, configured)
    # A business write owns SQLite. The collector must yield instead of taking
    # the application's RLock or blocking the live event loop on a busy timeout.
    with database.connect() as business:
        business.execute("BEGIN IMMEDIATE")
        business.execute("UPDATE calls SET current_stage='name' WHERE id=?", (call_id,))
        await recorder.start()
        recorder.begin(call_id)
        await asyncio.sleep(0.03)
        assert not recorder.queue.empty() or recorder.inflight_events
        business.commit()
    await recorder.stop()
    assert recorder.failed_events == 0
    assert database.get_call_diagnostics(call_id)["events"][0]["event"] == "call_started"


@pytest.mark.asyncio
async def test_interruption_and_audio_overflow_have_call_identity(configured):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="barge"))
    recorder = CallDiagnostics(database, configured)
    await recorder.start()
    connection = AudioSocketConnection(asyncio.StreamReader(), Writer(), configured)
    connection.trace = recorder.begin(call_id)
    connection.begin_provider_output()
    connection._capture_barge_in(int(1200).to_bytes(2, "little", signed=True) * 1600)
    connection.queue = asyncio.Queue(maxsize=1)
    connection._queue_audio(b"one")
    connection._queue_audio(b"two")
    await recorder.stop()
    events = database.get_call_diagnostics(call_id)["events"]
    assert [event["event"] for event in events] == ["call_started", "barge_in", "input_audio_dropped"]
    assert all(event["call_id"] == call_id for event in events)


@pytest.fixture
def portal(configured):
    previous = sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    database = module.runtime.database
    user_id = database.create_user("reviewer", "Call Reviewer", "unused", "manager", {}, None)
    token, csrf = database.create_session(user_id, False, 1)
    call_id = database.create_call(IntakeState(call_uuid="portal-feedback", name="Test Caller"))
    database.add_message(call_id, "caller", "I paused while spelling my email.")
    database.record_notification(call_id, "lead", "+12315550100", "failed", "Test delivery failure", "test-delivery")
    client = TestClient(module.app, base_url="https://testserver")
    client.cookies.set("floodman_session", token)
    yield module, client, call_id, csrf
    sys.modules.pop("app.main", None)
    if previous is not None:
        sys.modules["app.main"] = previous


@pytest.mark.parametrize("category", FEEDBACK_CATEGORIES)
def test_report_and_download_attach_existing_call_evidence(portal, category):
    module, client, call_id, csrf = portal
    response = client.post(f"/calls/{call_id}/feedback", data={
        "csrf_token": csrf, "category": category, "at_seconds": "24.5",
        "note": "Please review <script>unsafe()</script>.",
    }, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/calls/{call_id}")
    assert "Report a problem" in page.text and "Download diagnostics" in page.text
    assert "&lt;script&gt;" in page.text and "<script>unsafe()" not in page.text
    assert "Problem reported (1)" in client.get("/").text
    download = client.get(f"/calls/{call_id}/diagnostics")
    assert download.status_code == 200
    assert f'filename="call-{call_id}-diagnostics.json"' in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "no-store"
    report = download.json()
    feedback = report["diagnostics"]["feedback"][0]
    assert feedback["category"] == category and feedback["at_seconds"] == 24.5
    assert report["transcript"][0]["text"] == "I paused while spelling my email."
    assert report["delivery_attempts"][0]["status"] == "failed"
    assert report["coverage"]["audio_recording_included"] is False
    for secret in ("do-not-export-admin-secret", "do-not-export-smtp-secret", "password_hash", "csrf_token"):
        assert secret not in download.text


@pytest.mark.parametrize("category,offset,note", [
    ("invalid", "", ""), ("other", "nan", ""), ("other", "inf", ""),
    ("other", "-1", ""), ("other", "86401", ""), ("other", "", "x" * 2001),
])
def test_invalid_feedback_is_rejected(portal, category, offset, note):
    module, client, call_id, csrf = portal
    response = client.post(f"/calls/{call_id}/feedback", data={
        "csrf_token": csrf, "category": category, "at_seconds": offset, "note": note,
    }, follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert module.runtime.database.get_call_diagnostics(call_id)["feedback"] == []


def test_feedback_and_export_access_controls(portal):
    module, client, call_id, csrf = portal
    assert client.post(f"/calls/{call_id}/feedback", data={"csrf_token": "wrong"}).status_code == 403
    assert client.get("/calls/999999/diagnostics").status_code == 404
    assert client.post("/calls/999999/feedback", data={"csrf_token": csrf}).status_code == 404
    viewer_id = module.runtime.database.create_user("viewer", "Viewer", "unused", "viewer", {}, None)
    token, viewer_csrf = module.runtime.database.create_session(viewer_id, False, 1)
    client.cookies.set("floodman_session", token)
    assert client.get(f"/calls/{call_id}/diagnostics").status_code == 403
    assert client.post(f"/calls/{call_id}/feedback", data={"csrf_token": viewer_csrf}, follow_redirects=False).status_code == 303
    client.cookies.clear()
    assert client.get(f"/calls/{call_id}/diagnostics").status_code == 401
    assert client.post(f"/calls/{call_id}/feedback", data={"csrf_token": csrf}).status_code == 401


@pytest.mark.asyncio
async def test_call_deletion_removes_events_and_feedback_even_with_pending_writes(configured):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="delete"))
    database.add_call_feedback(call_id, None, "Reviewer", "other", None, "Test")
    recorder = CallDiagnostics(database, configured)
    recorder.begin(call_id)
    database.delete_call(call_id)
    await recorder.start()
    await recorder.stop()
    assert database.get_call_diagnostics(call_id)["event_count"] == 0
    assert database.get_call_diagnostics(call_id)["feedback"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["local", "tel_agent"])
async def test_live_protocol_drivers_save_the_same_call_timeline(configured, engine):
    configured.voice_engine = engine
    configured.post_tts_guard_ms = 0
    configured.endpoint_silence_ms = 100
    database = Database(configured.database_path)
    recorder = CallDiagnostics(database, configured)
    await recorder.start()

    class Core:
        settings = configured
        llm = SimpleNamespace(provider_name="test-local-model")

        def __init__(self):
            self.database = database

        def create_session(self, call_uuid, *_args):
            state = IntakeState(call_uuid=call_uuid)
            return SimpleNamespace(call_id=database.create_call(state), state=state, defer_assistant_recording=False)

        async def call_started(self, _session):
            pass

        def greeting(self):
            return "Hello. How can I help?"

        async def process(self, session, text, **_kwargs):
            database.add_message(session.call_id, "caller", text)
            session.state.completed = True
            return VoiceReply(text="Thank you. Goodbye.", end_call=True)

        async def no_input(self, _session):
            raise AssertionError("Expected caller audio")

        def record_assistant(self, session, text, **_kwargs):
            database.add_message(session.call_id, "assistant", text)

        async def disconnect(self, session, outcome):
            database.finish_call(session.call_id, outcome)

    class STT:
        streaming = False
        provider_name = "test-local-whisper"

        async def transcribe(self, *_args, **_kwargs):
            await asyncio.sleep(0.01)
            return "There is water in the basement."

        transcribe_local = transcribe

    class TTS:
        provider_name = "test-local-kokoro"

        async def synthesize(self, _text):
            return b"\x00\x00" * 160

    registry = SimpleNamespace(read_pre=lambda _uuid: {}, write_action=lambda *_args: None)
    core = Core()

    async def speak_turn(text_stream, *, sink, **_kwargs):
        text = "".join([piece async for piece in text_stream])
        await sink.write(b"\x00\x00" * 160)
        return SimpleNamespace(text=text, first_audio_ms=1, interrupted=False)

    if engine == "local":
        driver = AudioSocketServer(configured, core, STT(), TTS(), registry, recorder)
    else:
        driver = TelAgentPhoneAgent(core, STT(), core.llm, TTS(), registry,
                                    diagnostics=recorder, speak_turn=speak_turn)
    server = await asyncio.start_server(driver.handle, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    call_uuid = uuid.uuid4()
    try:
        writer.write(bytes([TYPE_UUID]) + struct.pack("!H", 16) + call_uuid.bytes)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(3), timeout=1)
        await reader.readexactly(struct.unpack("!H", header[1:])[0])
        # Wait for the paced greeting to finish before the caller answers.
        await asyncio.sleep(0.04)
        voice = int(1200).to_bytes(2, "little", signed=True) * 160
        for payload in [voice] * 10 + [b"\x00\x00" * 160] * 35:
            writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", len(payload)) + payload)
        await writer.drain()
        await asyncio.wait_for(reader.read(), timeout=2)
    finally:
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        await recorder.stop()
    call_id = database.call_id(str(call_uuid))
    events = database.get_call_diagnostics(call_id)["events"]
    names = [event["event"] for event in events]
    for required in ("call_started", "listening_started", "speech_started", "capture_ended", "stt_started",
                     "stt_completed", "processing_started", "processing_completed", "first_audio", "reply_completed", "call_ended"):
        assert required in names
    assert names[-1] == "call_ended" and events[-1]["details"]["outcome"] == "completed"
    assert summary({"events": events})["responses"] == 1
    assert database.get_call(call_id)["messages"]


@pytest.mark.asyncio
async def test_concurrent_provider_contexts_keep_events_with_the_right_call(configured):
    database = Database(configured.database_path)
    recorder = CallDiagnostics(database, configured)
    first_id = database.create_call(IntakeState(call_uuid="context-first"))
    second_id = database.create_call(IntakeState(call_uuid="context-second"))
    await recorder.start()

    async def process(call_id, reason):
        token = current_trace.set(recorder.begin(call_id))
        try:
            await asyncio.sleep(0)
            await asyncio.to_thread(emit_current, "tts_fallback", reason=reason, level="warning", error_type="RuntimeError")
        finally:
            current_trace.reset(token)

    await asyncio.gather(process(first_id, "first"), process(second_id, "second"))
    await recorder.stop()
    assert database.get_call_diagnostics(first_id)["events"][-1]["details"]["reason"] == "first"
    assert database.get_call_diagnostics(second_id)["events"][-1]["details"]["reason"] == "second"
    assert current_trace.get() is None


@pytest.mark.asyncio
async def test_real_model_and_voice_fallbacks_are_recorded_without_provider_errors(configured, monkeypatch):
    database = Database(configured.database_path)
    call_id = database.create_call(IntakeState(call_uuid="fallbacks"))
    recorder = CallDiagnostics(database, configured)
    await recorder.start()
    token = current_trace.set(recorder.begin(call_id))
    llm = LocalLLM(configured)
    tts = LocalTTS(configured)

    async def broken_chat(*_args, **_kwargs):
        raise RuntimeError("secret-provider-error-message")

    def broken_create(*_args, **_kwargs):
        raise RuntimeError("secret-provider-error-message")

    monkeypatch.setattr(llm, "_chat", broken_chat)
    tts._kokoro = SimpleNamespace(create=broken_create)
    monkeypatch.setattr(tts, "_espeak", lambda *_args: b"\x00\x00" * 160)
    try:
        assert await llm.extract("name", "Caller Name", {}) == {}
        assert await tts.synthesize("A short reply.")
    finally:
        current_trace.reset(token)
        await recorder.stop()

    diagnostics = database.get_call_diagnostics(call_id)
    names = [event["event"] for event in diagnostics["events"]]
    assert "llm_fallback" in names and "tts_fallback" in names and "tts_completed" in names
    assert "secret-provider-error-message" not in json.dumps(diagnostics)
