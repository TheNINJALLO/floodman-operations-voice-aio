# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tel-Agent's full-duplex phone driver over Floodman's private AudioSocket.

The transport and business authority remain Floodman-owned.  This module ports
Tel-Agent's conversation loop: a single streaming STT session, token-to-phrase
speech, immediate barge-in, measured first audio, and transcript commits for
only the speech that was actually delivered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.audiosocket import AudioSocketConnection
from app.intake_flow import contact_endpoint_stage
from app.models import VoiceReply
from app.registry import CallRegistry
from app.tel_agent_runtime import TelAgentLLM, TelAgentSTT, TelAgentTranscriptTurn, TelAgentTTS
from app.voice_core import CallSession, VoiceCore

logger = logging.getLogger(__name__)


class SpokenTurn(Protocol):
    text: str
    first_audio_ms: float | None
    interrupted: bool


SpeakTurn = Callable[..., Awaitable[SpokenTurn]]
_STREAM_ENDED = object()


@dataclass(slots=True)
class _StoppedTurn:
    text: str = ""
    first_audio_ms: float | None = None
    interrupted: bool = True


async def _single_text(text: str) -> AsyncIterator[str]:
    if text:
        yield text


class _AudioSocketSink:
    """Tel-Agent AudioSink backed by paced, private Asterisk AudioSocket PCM."""

    def __init__(self, connection: AudioSocketConnection):
        self.connection = connection

    async def write(self, chunk: bytes) -> None:
        await self.connection.write_provider_audio(chunk)

    async def flush(self) -> None:
        # AudioSocket writes are paced instead of buffered ahead. The connection's
        # barge event stops the current 20 ms write loop, so there is no remote
        # playback queue to flush.
        return None


class TelAgentPhoneAgent:
    """Own the call when VOICE_ENGINE=tel_agent; Floodman owns policy and data."""

    def __init__(
        self,
        core: VoiceCore,
        stt: TelAgentSTT,
        llm: TelAgentLLM,
        tts: TelAgentTTS,
        registry: CallRegistry,
        *,
        speak_turn: SpeakTurn | None = None,
    ):
        self.core = core
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.registry = registry
        self._speak_turn = speak_turn

    def _speaker(self) -> SpeakTurn:
        if self._speak_turn is None:
            # Kept lazy so local tests do not need to clone Tel-Agent. The image
            # contract imports this exact pinned upstream module during its build.
            from agent.session.turn import speak

            self._speak_turn = speak
        return self._speak_turn

    @staticmethod
    def _must_speak_exactly(reply: VoiceReply, session: CallSession, input_stage: str) -> bool:
        """Keep safety, transfer, confirmations, and closing language deterministic."""

        protected_wording = (
            "911",
            "not a service floodman offers",
            "can't complete a live transfer",
            "cannot complete a live transfer",
            "within 24 hours",
        )
        normalized_reply = reply.text.lower()
        return bool(
            reply.transfer_number
            or reply.end_call
            or reply.speech_parts
            or session.state.urgency == "emergency"
            or session.state.service_status == "unsupported"
            or input_stage.startswith("confirm_")
            or session.state.stage.startswith("confirm_")
            or any(phrase in normalized_reply for phrase in protected_wording)
        )

    def _history(self, session: CallSession) -> list[dict[str, str]]:
        call = self.core.database.get_call(session.call_id) or {}
        output: list[dict[str, str]] = []
        for item in call.get("messages") or []:
            role = "user" if item.get("role") == "caller" else "assistant"
            text = str(item.get("text") or "").strip()
            if text and role in {"user", "assistant"}:
                output.append({"role": role, "content": text})
        return output[-20:]

    async def _reply_stream(
        self,
        session: CallSession,
        caller_text: str,
        reply: VoiceReply,
        input_stage: str,
    ) -> AsyncIterator[str]:
        naturalize = os.getenv("TEL_AGENT_NATURALIZE", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not naturalize or self._must_speak_exactly(reply, session, input_stage):
            yield reply.text
            return

        stream = self.llm.stream_spoken_reply(
            caller_text=caller_text,
            required_reply=reply.text,
            state=session.state.to_dict(),
            history=self._history(session),
        )
        emitted = False
        async for piece in stream:
            if piece:
                emitted = True
                yield piece
        if not emitted:
            yield reply.text

    async def _speak(
        self,
        connection: AudioSocketConnection,
        text_stream: AsyncIterator[str],
        call_uuid: str,
        *,
        since: float | None = None,
    ) -> SpokenTurn:
        connection.begin_provider_output()
        try:
            if connection.closed or connection.barge_in_event.is_set():
                close = getattr(text_stream, "aclose", None)
                if close is not None:
                    await close()
                result: SpokenTurn = _StoppedTurn()
            else:
                result = await self._speaker()(
                    text_stream,
                    tts=self.tts,
                    sink=_AudioSocketSink(connection),
                    should_stop=lambda: connection.closed or connection.barge_in_event.is_set(),
                    since=since,
                )
        finally:
            interrupted = connection.barge_in_event.is_set()
            await connection.finish_provider_output(interrupted=interrupted)
        logger.info(
            "call_event call_uuid=%s stage=tel_agent_spoken_turn first_audio_ms=%s interrupted=%s audio_bytes=%d",
            call_uuid,
            round(result.first_audio_ms) if result.first_audio_ms is not None else "none",
            result.interrupted,
            connection.last_stream_audio_bytes,
        )
        return result

    async def _pump_transcripts(
        self,
        connection: AudioSocketConnection,
        output: asyncio.Queue[TelAgentTranscriptTurn | BaseException | object],
    ) -> None:
        try:
            async for turn in self.stt.stream_turns(connection.inbound_frames()):
                await output.put(turn)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await output.put(exc)
        finally:
            await output.put(_STREAM_ENDED)

    async def _local_turn(
        self, connection: AudioSocketConnection, session: CallSession
    ) -> TelAgentTranscriptTurn | None:
        contact = contact_endpoint_stage(session.state.stage)
        audio = await connection.utterance(contact=contact, stage=session.state.stage)
        if audio is None:
            return None
        if not audio:
            return TelAgentTranscriptTurn(text="")
        text = await self.stt.transcribe_local(audio, 8000, contact=contact)
        return TelAgentTranscriptTurn(text=text)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = AudioSocketConnection(reader, writer, self.core.settings)
        session: CallSession | None = None
        stream_task: asyncio.Task[None] | None = None
        outcome = "caller_hangup"
        call_uuid = ""
        streaming = self.stt.streaming
        transcript_queue: asyncio.Queue[TelAgentTranscriptTurn | BaseException | object] = (
            asyncio.Queue()
        )
        logger.info("call_event stage=tel_agent_tcp_accept peer=%s", writer.get_extra_info("peername"))
        try:
            call_uuid = await connection.start()
            metadata = self.registry.read_pre(call_uuid)
            session = self.core.create_session(
                call_uuid,
                metadata.get("caller_number", ""),
                metadata.get("called_number", ""),
            )
            session.defer_assistant_recording = True
            await self.core.call_started(session)
            logger.info(
                "call_event call_uuid=%s stage=tel_agent_session_created call_id=%s streaming_stt=%s",
                call_uuid,
                session.call_id,
                streaming,
            )

            if streaming:
                stream_task = asyncio.create_task(
                    self._pump_transcripts(connection, transcript_queue),
                    name=f"tel-agent-stt-{call_uuid}",
                )

            greeting = await self._speak(
                connection,
                _single_text(self.core.greeting()),
                call_uuid,
            )
            self.core.record_assistant(session, greeting.text)

            while not connection.closed:
                if streaming:
                    try:
                        item = await asyncio.wait_for(transcript_queue.get(), timeout=12.0)
                    except asyncio.TimeoutError:
                        turn = TelAgentTranscriptTurn(text="")
                    else:
                        if isinstance(item, BaseException):
                            logger.warning(
                                "call_event call_uuid=%s stage=streaming_stt_failed error=%s fallback=local_whisper",
                                call_uuid,
                                type(item).__name__,
                            )
                            streaming = False
                            if stream_task:
                                stream_task.cancel()
                                await asyncio.gather(stream_task, return_exceptions=True)
                                stream_task = None
                            recovery = VoiceReply(
                                text="I had trouble hearing that. Could you say that again?"
                            )
                            spoken = await self._speak(
                                connection, _single_text(recovery.text), call_uuid
                            )
                            self.core.record_assistant(session, spoken.text)
                            continue
                        if item is _STREAM_ENDED:
                            if connection.closed:
                                break
                            streaming = False
                            continue
                        assert isinstance(item, TelAgentTranscriptTurn)
                        turn = item
                else:
                    turn = await self._local_turn(connection, session)
                    if turn is None:
                        break

                turn_ended = time.perf_counter()
                caller_text = turn.text.strip()
                input_stage = session.state.stage
                if caller_text:
                    logger.info(
                        "call_event call_uuid=%s stage=tel_agent_final_transcript chars=%d confidence=%s language=%s",
                        call_uuid,
                        len(caller_text),
                        turn.confidence if turn.confidence is not None else "none",
                        turn.language or "unknown",
                    )
                    reply = await self.core.process(
                        session,
                        caller_text,
                        confidence=turn.confidence,
                        language=turn.language,
                    )
                else:
                    reply = await self.core.no_input(session)

                result = await self._speak(
                    connection,
                    self._reply_stream(session, caller_text, reply, input_stage),
                    call_uuid,
                    since=turn_ended,
                )
                self.core.record_assistant(
                    session,
                    result.text,
                    interrupted=result.interrupted,
                )
                if result.interrupted and caller_text and not reply.transfer_number and not reply.end_call:
                    self.core.note_prompt_interrupted(
                        session,
                        previous_stage=input_stage,
                        playback_fraction=0.0,
                    )
                if reply.transfer_number:
                    self.registry.write_action(
                        call_uuid, "transfer", reply.transfer_number, "assistant_transfer"
                    )
                    outcome = "transfer"
                    break
                if reply.end_call:
                    outcome = "completed" if session.state.completed else "no_input"
                    action = "completed" if session.state.completed else "fallback_callback"
                    self.registry.write_action(call_uuid, action, "", outcome)
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "call_event call_uuid=%s stage=tel_agent_phone_failed",
                call_uuid or "unknown",
            )
            if call_uuid:
                self.registry.write_action(
                    call_uuid, "technical_failure", "", "tel_agent_phone_error"
                )
            outcome = "error"
        finally:
            if stream_task:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            if session is not None:
                with contextlib.suppress(Exception):
                    await self.core.disconnect(session, outcome)
            await connection.close()
