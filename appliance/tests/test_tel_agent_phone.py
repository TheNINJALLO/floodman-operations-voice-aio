from __future__ import annotations

import asyncio
import struct
import uuid
from types import SimpleNamespace

import pytest

from app.models import IntakeState, VoiceReply
from app.audiosocket import TYPE_AUDIO, TYPE_UUID
from app.tel_agent_phone import TelAgentPhoneAgent


class FakeDatabase:
    def get_call(self, _call_id: int):
        return {"messages": [{"role": "caller", "text": "There is water downstairs"}]}


class FakeCore:
    database = FakeDatabase()


class FakeLLM:
    def __init__(self, pieces):
        self.pieces = pieces

    async def stream_spoken_reply(self, **_kwargs):
        for piece in self.pieces:
            yield piece


def agent(pieces=()):
    return TelAgentPhoneAgent(
        FakeCore(),
        SimpleNamespace(),
        FakeLLM(pieces),
        SimpleNamespace(),
        SimpleNamespace(),
        speak_turn=SimpleNamespace(),
    )


def session(stage="issue", urgency="normal"):
    return SimpleNamespace(
        call_id=7,
        state=IntakeState(call_uuid="test", stage=stage, urgency=urgency),
    )


@pytest.mark.asyncio
async def test_tel_agent_naturalizes_only_the_wording(monkeypatch) -> None:
    monkeypatch.setenv("TEL_AGENT_NATURALIZE", "true")
    phone = agent(("Absolutely. ", "Is this a home or a business?"))
    reply = VoiceReply(text="Is this a home or a business?")

    text = "".join([piece async for piece in phone._reply_stream(
        session(), "There is water downstairs", reply, "issue"
    )])

    assert text == "Absolutely. Is this a home or a business?"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "stage", "urgency"),
    [
        (VoiceReply(text="Call 911.", end_call=True), "issue", "emergency"),
        (VoiceReply(text="Is that correct?", speech_parts=("Josh", "Is that correct?")), "name", "normal"),
        (VoiceReply(text="I'll connect you now.", transfer_number="+12315550100"), "issue", "normal"),
        (VoiceReply(text="Is that correct?"), "confirm_email", "normal"),
        (VoiceReply(text="That is not a service Floodman offers."), "issue", "normal"),
    ],
)
async def test_tel_agent_never_rewrites_protected_or_exact_replies(
    monkeypatch, reply, stage, urgency
) -> None:
    monkeypatch.setenv("TEL_AGENT_NATURALIZE", "true")
    phone = agent(("unsafe rewrite",))

    text = "".join([piece async for piece in phone._reply_stream(
        session(stage, urgency), "yes", reply, stage
    )])

    assert text == reply.text


@pytest.mark.asyncio
async def test_tel_agent_falls_back_to_required_reply_when_model_is_empty(monkeypatch) -> None:
    monkeypatch.setenv("TEL_AGENT_NATURALIZE", "true")
    reply = VoiceReply(text="Which room is affected?")

    text = "".join([piece async for piece in agent()._reply_stream(
        session(), "Water is in my house", reply, "issue"
    )])

    assert text == reply.text


@pytest.mark.asyncio
async def test_tel_agent_phone_driver_owns_call_and_commits_spoken_turns() -> None:
    settings = SimpleNamespace(
        post_tts_guard_ms=0,
        barge_in_enabled=True,
        barge_in_min_speech_ms=160,
        barge_in_energy_threshold=325,
        barge_in_preroll_ms=240,
        contact_endpoint_silence_ms=100,
        endpoint_silence_ms=100,
        email_endpoint_silence_ms=1600,
        vad_energy_threshold=325,
        minimum_speech_ms=160,
        maximum_utterance_seconds=2,
    )

    class Core:
        def __init__(self):
            self.settings = settings
            self.llm = FakeLLM(())
            self.database = FakeDatabase()
            self.recorded = []
            self.disconnected = asyncio.Event()

        def create_session(self, *_args):
            return SimpleNamespace(
                call_id=9,
                state=IntakeState(call_uuid="driver-test"),
                defer_assistant_recording=False,
            )

        async def call_started(self, _session):
            return None

        @staticmethod
        def greeting():
            return "Hello from Floodman."

        async def process(self, _session, text, **_metadata):
            assert text == "There is water in the basement"
            return VoiceReply(text="I saved that for the team. Goodbye.", end_call=True)

        async def no_input(self, _session):
            raise AssertionError("speech should have been transcribed")

        def record_assistant(self, _session, text, **_metadata):
            self.recorded.append(text)

        async def disconnect(self, _session, outcome):
            assert outcome == "no_input"
            self.disconnected.set()

    class STT:
        streaming = False

        async def transcribe_local(self, *_args, **_kwargs):
            return "There is water in the basement"

    class Registry:
        def __init__(self):
            self.actions = []

        def read_pre(self, _call_uuid):
            return {"caller_number": "+12315550100", "called_number": "+12315550101"}

        def write_action(self, *values):
            self.actions.append(values)

    async def speak_turn(text_stream, *, sink, should_stop, **_kwargs):
        text = "".join([piece async for piece in text_stream])
        assert not should_stop()
        await sink.write(b"\x00\x00" * 160)
        return SimpleNamespace(text=text, first_audio_ms=25.0, interrupted=False)

    core = Core()
    registry = Registry()
    phone = TelAgentPhoneAgent(
        core, STT(), core.llm, SimpleNamespace(), registry, speak_turn=speak_turn
    )
    server = await asyncio.start_server(phone.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    call_uuid = uuid.uuid4()

    writer.write(bytes([TYPE_UUID]) + struct.pack("!H", 16) + call_uuid.bytes)
    await writer.drain()
    header = await asyncio.wait_for(reader.readexactly(3), timeout=1)
    await reader.readexactly(struct.unpack("!H", header[1:])[0])
    voiced = int(1200).to_bytes(2, "little", signed=True) * 160
    silence = b"\x00\x00" * 160
    for _ in range(10):
        writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", len(voiced)) + voiced)
    for _ in range(6):
        writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", len(silence)) + silence)
    await writer.drain()

    await asyncio.wait_for(core.disconnected.wait(), timeout=2)

    assert core.recorded == [
        "Hello from Floodman.",
        "I saved that for the team. Goodbye.",
    ]
    assert registry.actions == [(str(call_uuid), "fallback_callback", "", "no_input")]
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()
