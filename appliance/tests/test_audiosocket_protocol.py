import asyncio
import struct
import uuid
from types import SimpleNamespace

import pytest

from app.audiosocket import AudioSocketConnection, AudioSocketServer, TYPE_AUDIO, TYPE_HANGUP, TYPE_UUID
from app.models import VoiceReply


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
        barge_in_enabled=True,
        barge_in_min_speech_ms=160,
        barge_in_energy_threshold=325,
        barge_in_preroll_ms=240,
        email_endpoint_silence_ms=1600,
    )


class BufferWriter:
    def __init__(self):
        self.frames: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.frames.append(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def audio_frame(sample: int, milliseconds: int = 20) -> bytes:
    payload = int(sample).to_bytes(2, "little", signed=True) * int(8000 * milliseconds / 1000)
    return bytes([TYPE_AUDIO]) + struct.pack("!H", len(payload)) + payload


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


@pytest.mark.asyncio
async def test_confirmation_audio_contains_exact_brief_pause() -> None:
    class DistinctTTS:
        async def synthesize(self, text: str) -> bytes:
            return b"\x01\x00" * 2 if text == "I heard Josh Aldrich" else b"\x02\x00" * 2

    server = AudioSocketServer(settings(), FakeCore(), SimpleNamespace(), DistinctTTS(), FakeRegistry())
    reply = VoiceReply(
        text="I heard Josh Aldrich. Is that correct?",
        speech_parts=("I heard Josh Aldrich", "Is that correct?"),
        pause_between_parts_ms=300,
    )

    audio = await server._synthesize_reply(reply)

    assert audio[:4] == b"\x01\x00" * 2
    assert audio[4:-4] == b"\x00\x00" * 2400
    assert audio[-4:] == b"\x02\x00" * 2


@pytest.mark.asyncio
async def test_caller_speech_interrupts_playback_and_is_preserved() -> None:
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = AudioSocketConnection(reader, writer, settings())
    connection.reader_task = asyncio.create_task(connection._reader_loop())
    playback = asyncio.create_task(connection.speak(b"\x00\x00" * 8000))

    while len(writer.frames) < 3:
        await asyncio.sleep(0.01)
    for _ in range(10):
        reader.feed_data(audio_frame(1200))
    for _ in range(6):
        reader.feed_data(audio_frame(0))

    assert await asyncio.wait_for(playback, timeout=1) is True
    assert connection.last_playback_fraction < 1.0
    captured = await asyncio.wait_for(connection.utterance(contact=False), timeout=1)
    assert captured
    assert len(captured) >= 10 * 320
    assert len(writer.frames) < 50

    reader.feed_data(bytes([TYPE_HANGUP, 0, 0]))
    await asyncio.wait_for(connection.reader_task, timeout=1)
    await connection.close()


@pytest.mark.asyncio
async def test_line_noise_does_not_interrupt_playback() -> None:
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = AudioSocketConnection(reader, writer, settings())
    connection.reader_task = asyncio.create_task(connection._reader_loop())
    playback = asyncio.create_task(connection.speak(b"\x00\x00" * 3200))

    while len(writer.frames) < 3:
        await asyncio.sleep(0.01)
    for _ in range(12):
        reader.feed_data(audio_frame(100))

    assert await asyncio.wait_for(playback, timeout=1) is False
    assert len(writer.frames) == 20

    reader.feed_data(bytes([TYPE_HANGUP, 0, 0]))
    await asyncio.wait_for(connection.reader_task, timeout=1)
    await connection.close()


@pytest.mark.asyncio
async def test_speech_queued_during_processing_is_not_cleared_before_playback() -> None:
    reader = asyncio.StreamReader()
    writer = BufferWriter()
    connection = AudioSocketConnection(reader, writer, settings())
    connection.reader_task = asyncio.create_task(connection._reader_loop())

    for _ in range(10):
        reader.feed_data(audio_frame(1200))
    for _ in range(6):
        reader.feed_data(audio_frame(0))
    await asyncio.sleep(0.05)
    assert connection.queue.qsize() >= 10

    assert await connection.speak(b"\x00\x00" * 3200) is True
    assert writer.frames == []
    captured = await asyncio.wait_for(connection.utterance(contact=False), timeout=1)
    assert captured and len(captured) >= 10 * 320

    reader.feed_data(bytes([TYPE_HANGUP, 0, 0]))
    await asyncio.wait_for(connection.reader_task, timeout=1)
    await connection.close()


@pytest.mark.asyncio
async def test_email_endpoint_allows_a_natural_spelling_pause() -> None:
    connection = AudioSocketConnection(asyncio.StreamReader(), BufferWriter(), settings())
    capture = asyncio.create_task(connection.utterance(contact=True, stage="email"))

    for _ in range(10):
        connection._queue_audio(int(1200).to_bytes(2, "little", signed=True) * 160)
    for _ in range(50):  # One second is longer than other contact fields.
        connection._queue_audio(b"\x00\x00" * 160)
    await asyncio.sleep(0)
    assert not capture.done()

    for _ in range(10):
        connection._queue_audio(int(1200).to_bytes(2, "little", signed=True) * 160)
    for _ in range(80):
        connection._queue_audio(b"\x00\x00" * 160)

    audio = await asyncio.wait_for(capture, timeout=1)
    assert audio and len(audio) >= 150 * 320


@pytest.mark.asyncio
async def test_email_readback_uses_slower_tts_only_for_spelling() -> None:
    class SpeedTTS:
        def __init__(self): self.calls=[]
        async def synthesize(self, text: str, *, speed=None) -> bytes:
            self.calls.append((text,speed));return b"\x01\x00" * 2

    tts=SpeedTTS();server=AudioSocketServer(settings(),FakeCore(),SimpleNamespace(),tts,FakeRegistry())
    reply=VoiceReply(
        text="I heard j, o, at, e, x, dot, c, o, m. Is that correct?",
        speech_parts=("I heard j, o, at, e, x, dot, c, o, m","Is that correct?"),
        pause_between_parts_ms=300,
        speech_part_speeds=(0.78,None),
    )

    await server._synthesize_reply(reply)

    assert tts.calls==[(reply.speech_parts[0],0.78),(reply.speech_parts[1],None)]


def test_long_reply_is_split_at_natural_boundaries_for_responsive_tts() -> None:
    server = AudioSocketServer(settings(), FakeCore(), SimpleNamespace(), FakeTTS(), FakeRegistry())
    text = (
        "I have the details about the water entering the finished basement from the supply line. "
        "I also noted that the water is close to an electrical outlet and that the shutoff is accessible. "
        "Please keep everyone away from the affected area while I notify the emergency team."
    )

    parts = server._responsive_text_parts(text)

    assert len(parts) == 3
    assert " ".join(parts) == text
    assert all(len(part) <= 150 for part in parts)


@pytest.mark.asyncio
async def test_reply_playback_starts_while_later_phrase_is_still_synthesizing() -> None:
    class PipelinedTTS:
        def __init__(self):
            self.second_started = asyncio.Event()
            self.release_second = asyncio.Event()

        async def synthesize(self, text: str, *, speed=None) -> bytes:
            if text == "The remaining detail is still being generated.":
                self.second_started.set()
                await self.release_second.wait()
            return b"\x01\x00" * 3200

    tts = PipelinedTTS()
    server = AudioSocketServer(settings(), FakeCore(), SimpleNamespace(), tts, FakeRegistry())
    writer = BufferWriter()
    connection = AudioSocketConnection(asyncio.StreamReader(), writer, settings())
    reply = VoiceReply(
        text="I can help with that. The remaining detail is still being generated.",
        speech_parts=("I can help with that.", "The remaining detail is still being generated."),
    )

    playback = asyncio.create_task(server._speak_reply(connection, reply, "responsive-test"))
    await asyncio.wait_for(tts.second_started.wait(), timeout=1)
    for _ in range(50):
        if writer.frames:
            break
        await asyncio.sleep(0.01)

    assert writer.frames
    assert not tts.release_second.is_set()
    tts.release_second.set()
    assert await asyncio.wait_for(playback, timeout=2) is False
