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
        self.close_reason = "open"

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
                    self.close_reason = "asterisk_hangup_frame"
                    break
                if kind == TYPE_AUDIO and payload and not self.output_active:
                    with contextlib.suppress(asyncio.QueueFull):
                        self.queue.put_nowait(payload)
                elif kind == TYPE_ERROR:
                    self.close_reason = "asterisk_error_frame"
                    logger.warning("call_event stage=audiosocket_error code=%s", payload.hex())
                    break
        except asyncio.IncompleteReadError:
            self.close_reason = "socket_eof"
        except ConnectionResetError:
            self.close_reason = "socket_reset"
        except BrokenPipeError:
            self.close_reason = "broken_pipe"
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
        peer = writer.get_extra_info("peername")
        logger.info("call_event stage=tcp_accept peer=%s", peer)
        try:
            call_uuid = await connection.start()
            logger.info("call_event call_uuid=%s stage=uuid_received", call_uuid)
            metadata = self.registry.read_pre(call_uuid)
            session = self.core.create_session(call_uuid, metadata.get("caller_number", ""), metadata.get("called_number", ""))
            logger.info(
                "call_event call_uuid=%s stage=session_created call_id=%s channel_id=%s sip_call_id=%s",
                call_uuid,
                session.call_id,
                metadata.get("asterisk_channel_id", ""),
                metadata.get("sip_call_id", ""),
            )
            greeting = self.core.greeting()
            self.core.database.add_message(session.call_id, "assistant", greeting)
            started = time.monotonic()
            logger.info("call_event call_uuid=%s stage=greeting_synthesis_started", call_uuid)
            greeting_audio = await self.tts.synthesize(greeting)
            logger.info(
                "call_event call_uuid=%s stage=greeting_synthesis_completed duration_ms=%d audio_bytes=%d",
                call_uuid,
                int((time.monotonic() - started) * 1000),
                len(greeting_audio),
            )
            logger.info("call_event call_uuid=%s stage=first_greeting_frame", call_uuid)
            await connection.speak(greeting_audio)
            logger.info("call_event call_uuid=%s stage=last_greeting_frame", call_uuid)
            while not connection.closed:
                audio = await connection.utterance(contact=contact_endpoint_stage(session.state.stage))
                if audio is None:
                    break
                if not audio:
                    logger.info("call_event call_uuid=%s stage=no_input", call_uuid)
                    reply = await self.core.no_input(session)
                else:
                    logger.info("call_event call_uuid=%s stage=caller_audio_received audio_bytes=%d", call_uuid, len(audio))
                    try:
                        started = time.monotonic()
                        logger.info("call_event call_uuid=%s stage=stt_started", call_uuid)
                        transcript = await self.stt.transcribe(audio, 8000)
                        logger.info(
                            "call_event call_uuid=%s stage=stt_completed duration_ms=%d transcript_chars=%d",
                            call_uuid,
                            int((time.monotonic() - started) * 1000),
                            len(transcript),
                        )
                    except Exception:
                        logger.exception("call_event call_uuid=%s stage=stt_failed", call_uuid)
                        transcript = ""
                    started = time.monotonic()
                    logger.info("call_event call_uuid=%s stage=voice_core_started", call_uuid)
                    reply = await self.core.process(session, transcript) if transcript else await self.core.no_input(session)
                    logger.info(
                        "call_event call_uuid=%s stage=voice_core_completed duration_ms=%d",
                        call_uuid,
                        int((time.monotonic() - started) * 1000),
                    )
                started = time.monotonic()
                logger.info("call_event call_uuid=%s stage=tts_started", call_uuid)
                response_audio = await self.tts.synthesize(reply.text)
                logger.info(
                    "call_event call_uuid=%s stage=tts_completed duration_ms=%d audio_bytes=%d",
                    call_uuid,
                    int((time.monotonic() - started) * 1000),
                    len(response_audio),
                )
                logger.info("call_event call_uuid=%s stage=first_response_frame", call_uuid)
                await connection.speak(response_audio)
                if reply.transfer_number:
                    self.registry.write_action(call_uuid, "transfer", reply.transfer_number, "assistant_transfer")
                    outcome = "transfer"
                    break
                if reply.end_call:
                    outcome = "completed" if session.state.completed else "no_input"
                    action = "completed" if session.state.completed else "fallback_callback"
                    self.registry.write_action(call_uuid, action, "", outcome)
                    break
        except Exception:
            logger.exception("call_event call_uuid=%s stage=voice_core_failed", call_uuid or "unknown")
            if call_uuid:
                self.registry.write_action(call_uuid, "technical_failure", "", "voice_core_error")
            outcome = "error"
        finally:
            if session:
                await self.core.disconnect(session, outcome)
            logger.info(
                "call_event call_uuid=%s stage=socket_closed outcome=%s reason=%s",
                call_uuid or "unknown",
                outcome,
                connection.close_reason,
            )
            await connection.close()
