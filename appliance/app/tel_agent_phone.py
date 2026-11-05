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
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.audiosocket import AudioSocketConnection
from app.call_diagnostics import CallDiagnostics, current_trace, emit_current
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
        diagnostics: CallDiagnostics | None = None,
    ):
        self.core = core
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.registry = registry
        self._speak_turn = speak_turn
        self.diagnostics = diagnostics

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
        trial_wording = (
            getattr(getattr(self.core, "settings", None), "local_voice_stack", "standard") == "streaming_trial"
            and session.state.stage in {"affected_area", "timing_summary", "source_summary", "water_condition", "service_detail"}
            and len(reply.speech_parts) == 1 and reply.text.endswith("?")
        )
        protected_reply = replace(reply, speech_parts=()) if trial_wording else reply
        if not naturalize or self._must_speak_exactly(protected_reply, session, input_stage):
            yield reply.text
            return

        stream = self.llm.stream_spoken_reply(
            caller_text=caller_text,
            required_reply=reply.text,
            state=session.state.to_dict(),
            history=self._history(session),
        )
        pieces: list[str] = []
        started = time.perf_counter()
        emit_current("wording_started")
        try:
            try:
                budget_ms = float(os.getenv("TEL_AGENT_WORDING_BUDGET_MS", "1200"))
            except ValueError:
                budget_ms = 1200
            # Complete the short optional rewrite before playing it. A slow
            # model or broken stream must not leave a spoken half-question.
            async with asyncio.timeout(max(200, min(budget_ms, 3000)) / 1000):
                async for piece in stream:
                    if piece:
                        if not pieces:
                            emit_current("wording_first_text", duration_ms=(time.perf_counter() - started) * 1000)
                        pieces.append(piece)
            candidate = "".join(pieces).strip()
            if not candidate or ("?" in reply.text and not candidate.endswith("?")):
                candidate = reply.text
                emit_current("wording_fallback", reason="incomplete_reply", level="warning")
            if trial_wording:
                from app.trial_wording import valid_question_rewrite
                if not valid_question_rewrite(candidate, reply.text, session.state.stage):
                    candidate = reply.text
                    emit_current("wording_fallback", reason="question_changed", level="warning")
        except Exception as exc:
            candidate = reply.text
            emit_current("wording_fallback", reason="required_reply", level="warning", error_type=type(exc).__name__)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
            emit_current("wording_completed", duration_ms=(time.perf_counter() - started) * 1000)
        yield candidate

    async def _speak(
        self,
        connection: AudioSocketConnection,
        text_stream: AsyncIterator[str],
        call_uuid: str,
        *,
        since: float | None = None,
        reply: VoiceReply | None = None,
    ) -> SpokenTurn:
        connection.event("reply_started")
        connection.begin_provider_output(since=since)
        try:
            if connection.closed or connection.barge_in_event.is_set():
                close = getattr(text_stream, "aclose", None)
                if close is not None:
                    await close()
                result: SpokenTurn = _StoppedTurn()
            else:
                local = getattr(getattr(self.core, "settings", None), "local_voice_stack", "standard") == "streaming_trial"
                arguments = {}
                speaker = self._speaker() if not local else None
                if local:
                    from app.local_speaker import speak
                    speaker = speak
                    if reply is not None:
                        arguments = {"speech_parts": reply.speech_parts, "part_speeds": reply.speech_part_speeds,
                                     "pause_ms": reply.pause_between_parts_ms}
                result = await speaker(
                    text_stream,
                    tts=self.tts,
                    sink=_AudioSocketSink(connection),
                    should_stop=lambda: connection.closed or connection.barge_in_event.is_set(),
                    since=since,
                    **arguments,
                )
        finally:
            interrupted = connection.barge_in_event.is_set()
            await connection.finish_provider_output(interrupted=interrupted)
        # Barge-in can arrive during the final playback guard, after the
        # upstream speaker has already returned. Transport state is authoritative.
        result = _StoppedTurn(
            text=result.text if connection.last_stream_audio_bytes else "",
            first_audio_ms=result.first_audio_ms,
            interrupted=result.interrupted or connection.barge_in_event.is_set(),
        )
        connection.event("reply_completed", interrupted=result.interrupted,
                         audio_bytes=connection.last_stream_audio_bytes)
        if not connection.last_stream_audio_bytes and not result.interrupted and not connection.closed:
            connection.event("no_output_audio", level="warning")
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
        session: CallSession,
    ) -> None:
        try:
            arguments = {"get_stage": lambda: session.state.stage} if getattr(self.stt, "stage_aware", False) else {}
            async for turn in self.stt.stream_turns(connection.inbound_frames(), **arguments):
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
        audio_ended_at = time.perf_counter()
        if audio is None:
            return None
        if not audio:
            return TelAgentTranscriptTurn(text="")
        event = getattr(connection, "event", lambda *_args, **_kwargs: None)
        event("stt_started", stage=session.state.stage)
        text = await self.stt.transcribe_local(audio, 8000, contact=contact)
        event("stt_completed", duration_ms=(time.perf_counter() - audio_ended_at) * 1000, chars=len(text))
        return TelAgentTranscriptTurn(text=text, audio_ended_at=audio_ended_at, heard_audio=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = AudioSocketConnection(reader, writer, self.core.settings)
        session: CallSession | None = None
        stream_task: asyncio.Task[None] | None = None
        outcome = "caller_hangup"
        call_uuid = ""
        trace_token = None
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
            if self.diagnostics is not None:
                connection.trace = self.diagnostics.begin(session.call_id,
                    llm_provider=self.llm.provider_name, stt_provider=self.stt.provider_name,
                    tts_provider=self.tts.provider_name, streaming_stt=streaming)
                trace_token = current_trace.set(connection.trace)
            await self.core.call_started(session)
            logger.info(
                "call_event call_uuid=%s stage=tel_agent_session_created call_id=%s streaming_stt=%s",
                call_uuid,
                session.call_id,
                streaming,
            )

            if streaming:
                stream_task = asyncio.create_task(
                    self._pump_transcripts(connection, transcript_queue, session),
                    name=f"tel-agent-stt-{call_uuid}",
                )

            greeting = await self._speak(
                connection,
                _single_text(self.core.greeting()),
                call_uuid,
            )
            self.core.record_assistant(session, greeting.text, interrupted=greeting.interrupted)
            session.introduction_pending = greeting.interrupted
            last_input_at = time.perf_counter()

            while not connection.closed:
                if connection.trace is not None:
                    connection.trace.turn += 1
                if streaming:
                    try:
                        item = await asyncio.wait_for(transcript_queue.get(), timeout=12.0)
                    except asyncio.TimeoutError:
                        connection.event("no_input", reason="stream_timeout")
                        turn = TelAgentTranscriptTurn(text="")
                    else:
                        if isinstance(item, BaseException):
                            connection.event("stt_failed", level="error", error_type=type(item).__name__)
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

                # Include local recognition in first-audio latency, rather than
                # starting the clock after the expensive CPU work has finished.
                turn_ended = turn.audio_ended_at if turn.audio_ended_at is not None else time.perf_counter()
                caller_text = turn.text.strip()
                input_stage = session.state.stage
                if not caller_text and turn.heard_audio and not session.introduction_pending:
                    # An unrecognized breath/echo is not a twelve-second silence.
                    if time.perf_counter() - last_input_at < getattr(self.core.settings, "no_input_timeout_seconds", 12.0):
                        connection.event("unrecognized_audio", outcome="keep_listening")
                        continue
                if caller_text:
                    last_input_at = time.perf_counter()
                    logger.info(
                        "call_event call_uuid=%s stage=tel_agent_final_transcript chars=%d confidence=%s language=%s",
                        call_uuid,
                        len(caller_text),
                        turn.confidence if turn.confidence is not None else "none",
                        turn.language or "unknown",
                    )
                    processing_started = time.perf_counter()
                    connection.event("processing_started", stage=input_stage)
                    reply = await self.core.process(
                        session,
                        caller_text,
                        confidence=turn.confidence,
                        language=turn.language,
                    )
                    connection.event("processing_completed", duration_ms=(time.perf_counter() - processing_started) * 1000,
                                     stage=session.state.stage)
                else:
                    reply = await self.core.no_input(session)
                    last_input_at = time.perf_counter()

                if reply.wait_for_caller:
                    continue
                result = await self._speak(
                    connection,
                    self._reply_stream(session, caller_text, reply, input_stage),
                    call_uuid,
                    since=turn_ended,
                    reply=reply,
                )
                self.core.record_assistant(
                    session,
                    result.text,
                    interrupted=result.interrupted,
                )
                if result.interrupted and reply.text.startswith((self.core.greeting(), "You're through to Floodman, I'm Alex.")):
                    session.introduction_pending = True
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
                    connection.event("transfer_requested", outcome="action_saved")
                    outcome = "transfer"
                    break
                if reply.end_call:
                    if result.interrupted:
                        connection.event("farewell_interrupted", outcome="keep_listening")
                        continue
                    finalize = getattr(self.core, "finalize_completion", None)
                    if finalize is not None:
                        await finalize(session)
                    outcome = "completed" if session.state.completed else "no_input"
                    action = "completed" if session.state.completed else "fallback_callback"
                    self.registry.write_action(call_uuid, action, "", outcome)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            connection.event("call_failed", level="error", error_type=type(exc).__name__)
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
            connection.event("call_ended", outcome=outcome, reason=connection.close_reason,
                             dropped_frames=connection.dropped_frames)
            if trace_token is not None:
                current_trace.reset(trace_token)
