from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid
from pathlib import Path

from app.call_gate.classifier import CallGateClassifier
from app.call_gate.state_machine import GateStateMachine
from app.call_gate.transcribers import MetadataTranscriber, Transcriber, pcm_to_wav
from app.config import Settings
from app.db import Database
from app.models import GateState

logger = logging.getLogger(__name__)

TERMINATE = 0x00
UUID_FRAME = 0x01
DTMF_FRAME = 0x03
ERROR_FRAME = 0xFF
AUDIO_SAMPLE_RATES = {
    0x10: 8_000,
    0x11: 12_000,
    0x12: 16_000,
    0x13: 24_000,
    0x14: 32_000,
    0x15: 44_100,
    0x16: 48_000,
    0x17: 96_000,
    0x18: 192_000,
}


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    frame_type = header[0]
    length = struct.unpack(">H", header[1:])[0]
    payload = await reader.readexactly(length) if length else b""
    return frame_type, payload


def encode_frame(frame_type: int, payload: bytes = b"") -> bytes:
    return bytes([frame_type]) + struct.pack(">H", len(payload)) + payload


class AudioSocketGateServer:
    def __init__(self, settings: Settings, database: Database, transcriber: Transcriber):
        self.settings = settings
        self.database = database
        self.transcriber = transcriber
        self.classifier = CallGateClassifier(settings)
        self.server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._accept,
            self.settings.gate_host,
            self.settings.gate_port,
            limit=1_048_576,
        )
        logger.info(
            "Floodman call gate listening on %s:%s with %s transcription",
            self.settings.gate_host,
            self.settings.gate_port,
            self.transcriber.name,
        )

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        gate_uuid = ""
        started = time.monotonic()
        last_transcription = 0.0
        pcm = bytearray()
        sample_rate = 8_000
        transcript = ""
        try:
            first_type, first_payload = await asyncio.wait_for(read_frame(reader), timeout=3.0)
            if first_type != UUID_FRAME or len(first_payload) != 16:
                raise ValueError("AudioSocket connection did not begin with a 16-byte UUID frame")
            gate_uuid = str(uuid.UUID(bytes=first_payload))
            session = self.database.get_gate(gate_uuid)
            if not session:
                logger.warning("Unregistered AudioSocket UUID %s from %s", gate_uuid, peer)
                from app.models import GateRegistration

                self.database.register_gate(
                    GateRegistration(call_id=f"unregistered:{gate_uuid}", metadata={"peer": str(peer)}),
                    gate_uuid,
                )
                session = self.database.get_gate(gate_uuid) or {}

            self.database.update_gate_state(gate_uuid, GateState.LISTENING.value)
            machine = GateStateMachine(self.classifier)
            source_hint = str(session.get("source_hint") or "")
            did = str(session.get("did") or "")
            caller_number = str(session.get("caller_number") or "")

            while True:
                elapsed = time.monotonic() - started
                if elapsed >= self.settings.gate_max_seconds:
                    decision = machine.feed(
                        transcript,
                        source_hint=source_hint,
                        did=did,
                        caller_number=caller_number,
                        elapsed_seconds=elapsed,
                        timed_out=True,
                    )
                    self.database.save_gate_decision(gate_uuid, decision)
                    break

                if isinstance(self.transcriber, MetadataTranscriber) and elapsed >= max(
                    0.4, self.settings.gate_min_seconds
                ):
                    decision = machine.feed(
                        "",
                        source_hint=source_hint,
                        did=did,
                        caller_number=caller_number,
                        elapsed_seconds=elapsed,
                        timed_out=True,
                    )
                    self.database.save_gate_decision(gate_uuid, decision)
                    break

                try:
                    frame_type, payload = await asyncio.wait_for(read_frame(reader), timeout=0.25)
                except asyncio.TimeoutError:
                    frame_type, payload = -1, b""
                except asyncio.IncompleteReadError:
                    logger.info("AudioSocket caller disconnected during gate UUID %s", gate_uuid)
                    break

                if frame_type == TERMINATE:
                    break
                if frame_type == ERROR_FRAME:
                    raise RuntimeError(f"Asterisk AudioSocket error frame: {payload.hex()}")
                if frame_type in AUDIO_SAMPLE_RATES:
                    frame_rate = AUDIO_SAMPLE_RATES[frame_type]
                    if pcm and frame_rate != sample_rate:
                        logger.warning(
                            "AudioSocket sample rate changed from %s to %s for %s; resetting buffer",
                            sample_rate,
                            frame_rate,
                            gate_uuid,
                        )
                        pcm.clear()
                    sample_rate = frame_rate
                    pcm.extend(payload)
                elif frame_type == DTMF_FRAME:
                    logger.debug("Ignoring pre-greeting DTMF %r for %s", payload, gate_uuid)

                elapsed = time.monotonic() - started
                audio_seconds = len(pcm) / max(1, sample_rate * 2)
                due = elapsed - last_transcription >= self.settings.gate_transcribe_interval_seconds
                enough_audio = audio_seconds >= max(0.7, self.settings.gate_min_seconds)
                if due and enough_audio:
                    last_transcription = elapsed
                    try:
                        candidate = await self.transcriber.transcribe(bytes(pcm), sample_rate)
                        if candidate:
                            transcript = candidate
                        elif (
                            not transcript
                            and elapsed >= self.settings.gate_no_speech_timeout_seconds
                        ):
                            decision = machine.feed(
                                "",
                                source_hint=source_hint,
                                did=did,
                                caller_number=caller_number,
                                elapsed_seconds=elapsed,
                                timed_out=True,
                            )
                            decision.metadata["no_speech_fail_open"] = True
                            self.database.save_gate_decision(gate_uuid, decision)
                            break
                    except Exception as exc:
                        logger.exception("Gate transcription failed for %s: %s", gate_uuid, exc)
                        if elapsed >= self.settings.gate_min_seconds + 1.5:
                            decision = machine.feed(
                                transcript,
                                source_hint=source_hint,
                                did=did,
                                caller_number=caller_number,
                                elapsed_seconds=elapsed,
                                timed_out=True,
                            )
                            decision.metadata["transcription_error"] = type(exc).__name__
                            self.database.save_gate_decision(gate_uuid, decision)
                            break
                        continue

                    decision = machine.feed(
                        transcript,
                        source_hint=source_hint,
                        did=did,
                        caller_number=caller_number,
                        elapsed_seconds=elapsed,
                    )
                    self.database.update_gate_state(gate_uuid, decision.state.value, transcript)
                    if decision.ready:
                        self.database.save_gate_decision(gate_uuid, decision)
                        break

            if self.settings.config.get("gate", {}).get("save_audio", False) and pcm:
                audio_path = Path(self.settings.data_dir) / "gate-audio" / f"{gate_uuid}.wav"
                audio_path.write_bytes(pcm_to_wav(bytes(pcm), sample_rate))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Call gate session failed for %s: %s", gate_uuid or peer, exc)
            if gate_uuid:
                session = self.database.get_gate(gate_uuid) or {}
                decision = GateStateMachine(self.classifier).feed(
                    transcript,
                    source_hint=str(session.get("source_hint") or ""),
                    did=str(session.get("did") or ""),
                    caller_number=str(session.get("caller_number") or ""),
                    elapsed_seconds=time.monotonic() - started,
                    timed_out=True,
                )
                decision.metadata["gate_error"] = type(exc).__name__
                self.database.save_gate_decision(gate_uuid, decision)
        finally:
            try:
                writer.write(encode_frame(TERMINATE))
                await writer.drain()
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
