import asyncio
import struct
import uuid
from types import SimpleNamespace

import pytest

from app.audiosocket import AudioSocketServer, TYPE_AUDIO, TYPE_HANGUP, TYPE_UUID


class FakeDatabase:
    def add_message(self, *_args) -> None:
        return None


class FakeCore:
    def __init__(self, *, fail_session: bool = False):
        self.database = FakeDatabase()
        self.fail_session = fail_session
        self.disconnected = asyncio.Event()
        self.outcome = ""

    def create_session(self, *_args):
        if self.fail_session:
            raise RuntimeError("session setup failed")
        return SimpleNamespace(call_id=7, state=SimpleNamespace(stage="issue", completed=False))

    @staticmethod
    def greeting() -> str:
        return "Test greeting"

    async def disconnect(self, _session, outcome: str) -> None:
        self.outcome = outcome
        self.disconnected.set()


class FakeTTS:
    async def synthesize(self, _text: str) -> bytes:
        return b"\x00\x00" * 160


class FakeRegistry:
    def __init__(self):
        self.actions = []
        self.action_written = asyncio.Event()

    def read_pre(self, _call_uuid: str):
        return {
            "caller_number": "+12315550100",
            "called_number": "+12318668376",
            "asterisk_channel_id": "171.42",
            "sip_call_id": "call-id@example",
        }

    def write_action(self, call_uuid: str, action: str, number: str, reason: str) -> None:
        self.actions.append((call_uuid, action, number, reason))
        self.action_written.set()


def settings():
    return SimpleNamespace(
        audiosocket_host="127.0.0.1",
        audiosocket_port=0,
        post_tts_guard_ms=0,
        contact_endpoint_silence_ms=100,
        endpoint_silence_ms=100,
        vad_energy_threshold=325,
        minimum_speech_ms=160,
        maximum_utterance_seconds=1,
    )


async def connect(server: AudioSocketServer):
    assert server.server is not None
    port = server.server.sockets[0].getsockname()[1]
    return await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_audiosocket_sends_greeting_and_handles_hangup() -> None:
    core = FakeCore()
    registry = FakeRegistry()
    server = AudioSocketServer(settings(), core, SimpleNamespace(), FakeTTS(), registry)
    await server.start()
    reader, writer = await connect(server)
    call_id = uuid.uuid4()

    writer.write(bytes([TYPE_UUID]) + struct.pack("!H", 16) + call_id.bytes)
    await writer.drain()
    header = await asyncio.wait_for(reader.readexactly(3), timeout=1)
    kind, size = header[0], struct.unpack("!H", header[1:])[0]
    payload = await reader.readexactly(size)

    assert kind == TYPE_AUDIO
    assert payload == b"\x00\x00" * 160

    writer.write(bytes([TYPE_HANGUP, 0, 0]))
    await writer.drain()
    await asyncio.wait_for(core.disconnected.wait(), timeout=1)
    assert core.outcome == "caller_hangup"
    assert registry.actions == []

    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_audiosocket_records_technical_failure_action() -> None:
    core = FakeCore(fail_session=True)
    registry = FakeRegistry()
    server = AudioSocketServer(settings(), core, SimpleNamespace(), FakeTTS(), registry)
    await server.start()
    reader, writer = await connect(server)
    call_id = uuid.uuid4()

    writer.write(bytes([TYPE_UUID]) + struct.pack("!H", 16) + call_id.bytes)
    await writer.drain()
    await asyncio.wait_for(registry.action_written.wait(), timeout=1)
    assert registry.actions == [(str(call_id), "technical_failure", "", "voice_core_error")]
    assert await asyncio.wait_for(reader.read(), timeout=1) == b""

    writer.close()
    await writer.wait_closed()
    await server.stop()
