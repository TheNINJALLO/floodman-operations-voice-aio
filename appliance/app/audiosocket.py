from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
import uuid

from app.audio import chunk_pcm, rms
from app.config import Settings
from app.intake_flow import contact_endpoint_stage
from app.registry import CallRegistry
from app.stt import LocalSTT
from app.tts import LocalTTS
from app.voice_core import VoiceCore

logger = logging.getLogger(__name__)

TYPE_HANGUP = 0x00
TYPE_UUID = 0x01
TYPE_AUDIO = 0x10
TYPE_ERROR = 0xFF


class AudioSocketConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, settings: Settings):
        self.reader = reader
        self.writer = writer
        self.settings = settings
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1000)
        self.output_active = False
        self.closed = False
        self.reader_task: asyncio.Task | None = None

    async def read_frame(self) -> tuple[int, bytes]:
        header = await self.reader.readexactly(3)
        kind, size = header[0], struct.unpack("!H", header[1:])[0]
        return kind, await self.reader.readexactly(size) if size else b""

    async def start(self) -> str:
        kind, payload = await asyncio.wait_for(self.read_frame(), timeout=5.0)
        if kind != TYPE_UUID or len(payload) != 16:
            raise ValueError("AudioSocket did not begin with a 16-byte UUID frame")
        call_uuid = str(uuid.UUID(bytes=payload))
        self.reader_task = asyncio.create_task(self._reader_loop())
        return call_uuid

    async def _reader_loop(self) -> None:
        try:
            while True:
                kind, payload = await self.read_frame()
                if kind == TYPE_HANGUP:
                    break
                if kind == TYPE_AUDIO and payload and not self.output_active:
                    with contextlib.suppress(asyncio.QueueFull):
                        self.queue.put_nowait(payload)
                elif kind == TYPE_ERROR:
                    logger.warning("AudioSocket error frame: %r", payload)
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)

    async def clear_audio(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def speak(self, pcm16le: bytes) -> None:
        if not pcm16le or self.closed:
            return
        self.output_active = True
        await self.clear_audio()
        try:
            for chunk in chunk_pcm(pcm16le, 8000, 20):
                self.writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", len(chunk)) + chunk)
                await self.writer.drain()
                await asyncio.sleep(0.020)
        finally:
            await asyncio.sleep(max(0, self.settings.post_tts_guard_ms) / 1000)
            await self.clear_audio()
            self.output_active = False

    async def utterance(self, *, contact: bool) -> bytes | None:
        endpoint_ms = self.settings.contact_endpoint_silence_ms if contact else self.settings.endpoint_silence_ms
        initial_timeout = 12.0
        speech = bytearray()
        speaking = False
        speech_ms = 0.0
        silence_ms = 0.0
        started = time.monotonic()
        while not self.closed:
            try:
                frame = await asyncio.wait_for(self.queue.get(), timeout=initial_timeout if not speaking else 2.0)
            except asyncio.TimeoutError:
                return b"" if not speaking else bytes(speech)
            if frame is None:
                return None
            duration_ms = (len(frame) / 2 / 8000) * 1000
            energy = rms(frame)
            voiced = energy >= self.settings.vad_energy_threshold
            if voiced:
                speaking = True
                silence_ms = 0.0
                speech_ms += duration_ms
                speech.extend(frame)
            elif speaking:
                silence_ms += duration_ms
                speech.extend(frame)
                if speech_ms >= self.settings.minimum_speech_ms and silence_ms >= endpoint_ms:
                    break
            if time.monotonic() - started >= self.settings.maximum_utterance_seconds:
                break
        return bytes(speech)

    async def close(self) -> None:
        self.closed = True
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


class AudioSocketServer:
    def __init__(self, settings: Settings, core: VoiceCore, stt: LocalSTT, tts: LocalTTS, registry: CallRegistry):
        self.settings = settings
        self.core = core
        self.stt = stt
        self.tts = tts
        self.registry = registry
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, self.settings.audiosocket_host, self.settings.audiosocket_port)
        logger.info("Floodman AudioSocket listening on %s:%s", self.settings.audiosocket_host, self.settings.audiosocket_port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = AudioSocketConnection(reader, writer, self.settings)
        session = None
        outcome = "caller_hangup"
        call_uuid = ""
        try:
            call_uuid = await connection.start()
            metadata = self.registry.read_pre(call_uuid)
            session = self.core.create_session(call_uuid, metadata.get("caller_number", ""), metadata.get("called_number", ""))
            greeting = self.core.greeting()
            self.core.database.add_message(session.call_id, "assistant", greeting)
            await connection.speak(await self.tts.synthesize(greeting))
            while not connection.closed:
                audio = await connection.utterance(contact=contact_endpoint_stage(session.state.stage))
                if audio is None:
                    break
                if not audio:
                    reply = await self.core.no_input(session)
                else:
                    try:
                        transcript = await self.stt.transcribe(audio, 8000)
                    except Exception:
                        logger.exception("Speech recognition failed")
                        transcript = ""
                    reply = await self.core.process(session, transcript) if transcript else await self.core.no_input(session)
                await connection.speak(await self.tts.synthesize(reply.text))
                if reply.transfer_number:
                    self.registry.write_action(call_uuid, "transfer", reply.transfer_number, "assistant_transfer")
                    outcome = "transfer"
                    break
                if reply.end_call:
                    self.registry.write_action(call_uuid, "hangup", "", "assistant_completed")
                    outcome = "completed" if session.state.completed else "no_input"
                    break
        except Exception:
            logger.exception("AudioSocket call failed")
            if call_uuid:
                self.registry.write_action(call_uuid, "hangup", "", "voice_core_error")
            outcome = "error"
            if not connection.closed:
                with contextlib.suppress(Exception):
                    await connection.speak(await self.tts.synthesize("I'm having a technical issue. I will send the information I have to the team. Goodbye."))
        finally:
            if session:
                await self.core.disconnect(session, outcome)
            await connection.close()
