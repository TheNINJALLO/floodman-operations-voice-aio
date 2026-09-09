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
from app.models import VoiceReply
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
        self.barge_in_event = asyncio.Event()
        self._barge_audio = bytearray()
        self._barge_voiced_ms = 0.0
        self._barge_silence_ms = 0.0

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
                if kind == TYPE_AUDIO and payload:
                    if self.output_active and self.settings.barge_in_enabled:
                        self._capture_barge_in(payload)
                    elif not self.output_active:
                        self._queue_audio(payload)
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

    def _queue_audio(self, payload: bytes) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(payload)

    @staticmethod
    def _duration_ms(payload: bytes) -> float:
        return (len(payload) / 2 / 8000) * 1000

    def _reset_barge_in(self) -> None:
        self._barge_audio.clear()
        self._barge_voiced_ms = 0.0
        self._barge_silence_ms = 0.0

    def _capture_barge_in(self, payload: bytes) -> None:
        if self.barge_in_event.is_set():
            self._queue_audio(payload)
            return

        duration_ms = self._duration_ms(payload)
        self._barge_audio.extend(payload)
        retained_ms = self.settings.barge_in_preroll_ms + self.settings.barge_in_min_speech_ms
        maximum_bytes = max(2, int(8000 * 2 * retained_ms / 1000))
        if len(self._barge_audio) > maximum_bytes:
            del self._barge_audio[: len(self._barge_audio) - maximum_bytes]

        energy = rms(payload)
        if energy >= self.settings.barge_in_energy_threshold:
            self._barge_voiced_ms += duration_ms
            self._barge_silence_ms = 0.0
        elif self._barge_voiced_ms:
            self._barge_silence_ms += duration_ms
            if self._barge_silence_ms > 120:
                self._barge_voiced_ms = 0.0
                self._barge_silence_ms = 0.0

        if self._barge_voiced_ms >= self.settings.barge_in_min_speech_ms:
            buffered = bytes(self._barge_audio)
            self._reset_barge_in()
            self.barge_in_event.set()
            self._queue_audio(buffered)
            logger.info(
                "call_event stage=barge_in_detected energy=%d buffered_audio_bytes=%d",
                energy,
                len(buffered),
            )

    def _queued_speech_waiting(self) -> bool:
        """Preserve speech received while the previous turn was being processed."""
        frames: list[bytes] = []
        while True:
            try:
                frame = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if frame is None:
                continue
            frames.append(frame)

        for frame in frames:
            self._capture_barge_in(frame)

        if self.barge_in_event.is_set():
            logger.info("call_event stage=queued_barge_in_detected buffered_frames=%d", len(frames))
            return True
        return False

    async def clear_audio(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def speak(self, pcm16le: bytes) -> bool:
        if not pcm16le or self.closed:
            return False
        self.barge_in_event.clear()
        self._reset_barge_in()
        if self.settings.barge_in_enabled and self._queued_speech_waiting():
            return True
        self.output_active = True
        await self.clear_audio()
        interrupted = False
        try:
            for chunk in chunk_pcm(pcm16le, 8000, 20):
                if self.closed or self.barge_in_event.is_set():
                    interrupted = self.barge_in_event.is_set()
                    break
                self.writer.write(bytes([TYPE_AUDIO]) + struct.pack("!H", len(chunk)) + chunk)
                await self.writer.drain()
                await asyncio.sleep(0.020)
        finally:
            if not interrupted:
                await asyncio.sleep(max(0, self.settings.post_tts_guard_ms) / 1000)
                interrupted = self.barge_in_event.is_set()
            if not interrupted:
                await self.clear_audio()
                self._reset_barge_in()
            self.output_active = False
        return interrupted

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
        self.active_calls = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self.handle, self.settings.audiosocket_host, self.settings.audiosocket_port)
        logger.info("Floodman AudioSocket listening on %s:%s", self.settings.audiosocket_host, self.settings.audiosocket_port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _synthesize_reply(self, reply: VoiceReply) -> bytes:
        parts = tuple(part for part in reply.speech_parts if str(part).strip())
        if len(parts) < 2:
            return await self.tts.synthesize(reply.text)
        audio_parts = [await self.tts.synthesize(part) for part in parts]
        pause_samples = max(0, int(8000 * reply.pause_between_parts_ms / 1000))
        pause = b"\x00\x00" * pause_samples
        return pause.join(audio_parts)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = AudioSocketConnection(reader, writer, self.settings)
        session = None
        outcome = "caller_hangup"
        call_uuid = ""
        peer = writer.get_extra_info("peername")
        logger.info("call_event stage=tcp_accept peer=%s", peer)
        self.active_calls += 1
        try:
            call_uuid = await connection.start()
            logger.info("call_event call_uuid=%s stage=uuid_received", call_uuid)
            metadata = self.registry.read_pre(call_uuid)
            session = self.core.create_session(call_uuid, metadata.get("caller_number", ""), metadata.get("called_number", ""))
            call_started = getattr(self.core, "call_started", None)
            if call_started is not None:
                await call_started(session)
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
            greeting_interrupted = await connection.speak(greeting_audio)
            logger.info(
                "call_event call_uuid=%s stage=last_greeting_frame interrupted=%s",
                call_uuid,
                greeting_interrupted,
            )
            while not connection.closed:
                contact = contact_endpoint_stage(session.state.stage)
                audio = await connection.utterance(contact=contact)
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
                        transcript = await self.stt.transcribe(audio, 8000, contact=contact)
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
                response_audio = await self._synthesize_reply(reply)
                logger.info(
                    "call_event call_uuid=%s stage=tts_completed duration_ms=%d audio_bytes=%d",
                    call_uuid,
                    int((time.monotonic() - started) * 1000),
                    len(response_audio),
                )
                logger.info("call_event call_uuid=%s stage=first_response_frame", call_uuid)
                response_interrupted = await connection.speak(response_audio)
                logger.info(
                    "call_event call_uuid=%s stage=last_response_frame interrupted=%s",
                    call_uuid,
                    response_interrupted,
                )
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
            self.active_calls = max(0, self.active_calls - 1)
