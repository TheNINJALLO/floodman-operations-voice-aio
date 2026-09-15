# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run Tel-Agent on Floodman's private Asterisk AudioSocket connection.

This adapter is deliberately a separate process.  Tel-Agent supplies the streaming
provider and turn primitives; Floodman's authenticated loopback API remains the only
way the model can reach business data or policy-controlled operations.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from app.ava.agents import AGENTS, AgentDefinition

LOGGER = logging.getLogger("floodman.tel_agent")

TERMINATE = 0x00
UUID_FRAME = 0x01
DTMF_FRAME = 0x03
ERROR_FRAME = 0xFF
SLIN8_FRAME = 0x10
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_SECONDS = FRAME_SAMPLES / 8_000
MAX_TOOL_RESULT_CHARS = 12_000
MAX_TOOL_ROUNDS = 3

TOOL_PATHS = {
    "floodman_lookup_customer": "/internal/tools/lookup-customer",
    "floodman_verify_customer": "/internal/tools/verify-customer",
    "floodman_create_lead": "/internal/tools/create-lead",
    "floodman_classify_service": "/internal/tools/classify-service",
    "floodman_capture_intake_progress": "/internal/tools/capture-intake-progress",
    "floodman_submit_intake": "/internal/tools/submit-intake",
    "floodman_create_emergency_case": "/internal/tools/create-emergency-case",
    "floodman_check_availability": "/internal/tools/check-availability",
    "floodman_schedule_inspection": "/internal/tools/schedule-inspection",
    "floodman_reschedule_inspection": "/internal/tools/reschedule-inspection",
    "floodman_send_photo_upload_link": "/internal/tools/send-photo-upload-link",
    "floodman_get_billing_summary": "/internal/tools/get-billing-summary",
    "floodman_send_payment_link": "/internal/tools/send-payment-link",
    "floodman_create_callback_task": "/internal/tools/create-callback-task",
    "floodman_record_disposition": "/internal/tools/record-disposition",
    "floodman_opt_out": "/internal/tools/opt-out",
    "floodman_record_security_event": "/internal/tools/security-event",
    "floodman_search_knowledge": "/internal/tools/search-knowledge",
    "floodman_public_business_information": "/internal/tools/public-business-information",
    "floodman_check_service_area": "/internal/tools/check-service-area",
}


@dataclass(slots=True)
class DeepgramUtterance:
    kind: str
    text: str
    confidence: float | None = None
    language: str | None = None


class DeepgramUtteranceAccumulator:
    """Combine stable Deepgram segments until speech endpointing closes the turn."""

    def __init__(self, fallback_language: str) -> None:
        self.fallback_language = fallback_language
        self.segments: list[str] = []
        self.confidences: list[float] = []
        self.language = fallback_language

    def _combined(self, current: str = "") -> str:
        return " ".join((*self.segments, current) if current else self.segments).strip()

    def _finish(self) -> DeepgramUtterance | None:
        text = self._combined()
        if not text:
            return None
        confidence = (
            sum(self.confidences) / len(self.confidences) if self.confidences else None
        )
        result = DeepgramUtterance("final", text, confidence, self.language)
        self.segments.clear()
        self.confidences.clear()
        self.language = self.fallback_language
        return result

    def feed(self, raw: str | bytes) -> DeepgramUtterance | None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if message.get("type") == "UtteranceEnd":
            return self._finish()
        if message.get("type") != "Results":
            return None
        channel = message.get("channel") if isinstance(message.get("channel"), dict) else {}
        alternatives = channel.get("alternatives") or []
        if not alternatives or not isinstance(alternatives[0], dict):
            return self._finish() if message.get("speech_final") else None
        alternative = alternatives[0]
        text = str(alternative.get("transcript") or "").strip()
        language = channel.get("detected_language")
        if isinstance(language, str) and language:
            self.language = language

        if message.get("is_final"):
            if text:
                self.segments.append(text)
                confidence = alternative.get("confidence")
                if isinstance(confidence, int | float):
                    self.confidences.append(float(confidence))
            if message.get("speech_final"):
                return self._finish()
            combined = self._combined()
            return DeepgramUtterance("partial", combined) if combined else None

        combined = self._combined(text)
        return DeepgramUtterance("partial", combined) if combined else None

    def finish(self) -> DeepgramUtterance | None:
        return self._finish()


def encode_frame(frame_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > 65_535:
        raise ValueError("AudioSocket payload exceeds the protocol frame limit")
    return bytes((frame_type,)) + struct.pack(">H", len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    length = struct.unpack(">H", header[1:])[0]
    return header[0], await reader.readexactly(length) if length else b""


def mulaw_to_slin8(payload: bytes) -> bytes:
    """Decode G.711 mu-law into little-endian signed linear 16-bit samples."""
    output = bytearray(len(payload) * 2)
    offset = 0
    for encoded in payload:
        value = (~encoded) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        struct.pack_into("<h", output, offset, max(-32_768, min(32_767, sample)))
        offset += 2
    return bytes(output)


def _json_schema(parameters: Any) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    type_map = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
    }
    for item in parameters if isinstance(parameters, list) else []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        name = str(item["name"]).strip()
        definition: dict[str, Any] = {
            "type": type_map.get(str(item.get("type") or "string").lower(), "string"),
            "description": str(item.get("description") or "").strip(),
        }
        if "default" in item:
            definition["default"] = item["default"]
        properties[name] = definition
        if bool(item.get("required")):
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def load_tool_definitions(path: Path) -> dict[str, dict[str, Any]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    configured = value.get("in_call_tools", {}) if isinstance(value, dict) else {}
    if not isinstance(configured, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, item in configured.items():
        if name not in TOOL_PATHS or not isinstance(item, dict) or not item.get("enabled", False):
            continue
        configured_url = str(item.get("url") or "").strip()
        if configured_url and not configured_url.endswith(TOOL_PATHS[name]):
            raise ValueError(f"Tool {name} is configured for an unexpected internal path")
        result[name] = {
            "description": str(item.get("description") or "").strip(),
            "parameters": _json_schema(item.get("parameters")),
            "timeout": max(1.0, min(15.0, float(item.get("timeout_ms") or 6000) / 1000)),
        }
    return result


def selected_agent(slug: str) -> AgentDefinition:
    by_slug = {agent.slug: agent for agent in AGENTS}
    return by_slug.get(slug) or by_slug["floodman_inbound"]


@dataclass(slots=True)
class SessionContext:
    gate_uuid: str
    call_id: str
    caller_number: str
    direction: str
    agent: AgentDefinition
    provider: str
    opening_transcript: str = ""
    campaign_id: str = ""
    lead_id: str = ""
    customer_id: str = ""


@dataclass(slots=True)
class TurnControl:
    stop_speaking: bool = False
    end_requested: bool = False
    transferred: bool = False
    direct_message: str = ""
    sequence: int = 0


class ControlClient:
    def __init__(self, base_url: str, token: str) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("Tel-Agent control API must use loopback HTTP")
        if not token:
            raise ValueError("INTERNAL_TOKEN is required for Tel-Agent")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Internal-Token": token},
            timeout=httpx.Timeout(8.0, connect=3.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ready(self) -> None:
        probe_uuid = "00000000-0000-0000-0000-000000000000"
        response = await self._client.get(f"/internal/gate/decision/{probe_uuid}")
        response.raise_for_status()

    async def session(self, gate_uuid: str) -> SessionContext:
        response = await self._client.get(f"/internal/gate/decision/{gate_uuid}")
        response.raise_for_status()
        data = response.json()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        slug = str(data.get("agent") or metadata.get("requested_agent") or "floodman_inbound")
        if metadata.get("requested_agent"):
            slug = str(metadata["requested_agent"])
        return SessionContext(
            gate_uuid=gate_uuid,
            call_id=str(data.get("call_id") or gate_uuid),
            caller_number=str(data.get("caller_number") or metadata.get("destination") or ""),
            direction=str(data.get("direction") or "inbound"),
            agent=selected_agent(slug),
            provider=str(data.get("provider") or "tel-agent"),
            opening_transcript=str(data.get("opening_transcript") or "").strip(),
            campaign_id=str(metadata.get("campaign_id") or ""),
            lead_id=str(metadata.get("lead_id") or metadata.get("job_id") or ""),
            customer_id=str(metadata.get("customer_id") or ""),
        )

    async def persist_turn(
        self,
        context: SessionContext,
        *,
        sequence: int,
        role: str,
        text: str,
        confidence: float | None = None,
        language: str | None = None,
        interrupted: bool = False,
    ) -> None:
        response = await self._client.post(
            "/internal/tel-agent/turn",
            json={
                "call_id": context.call_id,
                "direction": context.direction,
                "sequence": sequence,
                "role": role,
                "text": text,
                "confidence": confidence,
                "language": language,
                "interrupted": interrupted,
                "engine": "tel-agent",
            },
        )
        response.raise_for_status()

    async def run_tool(
        self,
        context: SessionContext,
        control: TurnControl,
        name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> str:
        if name == "hangup_call":
            control.end_requested = True
            return json.dumps({"ok": True, "will_hangup": True})
        if name == "transfer":
            destination = str(arguments.get("destination") or "live_reception")
            response = await self._client.post(
                "/internal/tel-agent/transfer",
                json={"gate_uuid": context.gate_uuid, "destination": destination},
                timeout=timeout,
            )
            data = response.json()
            if response.is_success and data.get("ok"):
                control.end_requested = True
                control.transferred = True
            return json.dumps(data, ensure_ascii=False)[:MAX_TOOL_RESULT_CHARS]

        path = TOOL_PATHS[name]
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        customer_id = str(arguments.get("customer_id") or context.customer_id or "")
        response = await self._client.post(
            path,
            json={
                "call_id": context.call_id,
                "customer_id": customer_id,
                "caller_number": context.caller_number,
                "idempotency_key": (
                    f"tel-agent:{context.call_id}:{control.sequence}:{name}:{digest}"
                ),
                "data": arguments,
            },
            timeout=timeout,
        )
        data = response.json()
        if name == "floodman_capture_intake_progress" and response.is_success:
            safe_message = str(data.get("safe_message") or "").strip()
            if safe_message:
                control.direct_message = safe_message
            control.end_requested = bool(data.get("submitted"))
        if response.status_code >= 400:
            data = {
                "ok": False,
                "error": "internal_tool_rejected",
                "safe_message": "That action is unavailable. Continue safely or offer a callback.",
            }
        return json.dumps(data, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]

    async def fallback_on_failure(self, context: SessionContext, reason: str) -> bool:
        try:
            response = await self._client.post(
                "/internal/tel-agent/fallback",
                json={"gate_uuid": context.gate_uuid, "reason": reason[:80]},
                timeout=4.0,
            )
            return bool(response.is_success and response.json().get("ok"))
        except Exception:
            return False

    async def transfer_on_failure(self, context: SessionContext) -> bool:
        try:
            response = await self._client.post(
                "/internal/tel-agent/transfer",
                json={"gate_uuid": context.gate_uuid, "destination": "live_reception"},
                timeout=4.0,
            )
            return bool(response.is_success and response.json().get("ok"))
        except Exception:
            return False

    async def completed(
        self,
        context: SessionContext,
        transcript: list[dict[str, Any]],
        *,
        duration: float,
        outcome: str,
        error_type: str = "",
    ) -> None:
        response = await self._client.post(
            "/internal/ava/call-completed",
            json={
                "call_id": context.call_id,
                "caller_number": context.caller_number,
                "direction": context.direction,
                "agent": context.agent.slug,
                "provider": "tel-agent",
                "campaign_id": context.campaign_id,
                "lead_id": context.lead_id,
                "conversation_id": context.gate_uuid,
                "duration": duration,
                "outcome": outcome,
                "summary": "",
                "transcript": transcript,
                "metadata": {
                    "context": context.agent.slug,
                    "engine": "tel-agent",
                    "transport": "asterisk_audiosocket",
                    "error_type": error_type,
                },
            },
        )
        response.raise_for_status()


class AudioSocketRoom:
    """Bidirectional AudioSocket room with a small, flushable playback queue."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.inbound_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=500)
        self.playback_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=500)
        self.closed = asyncio.Event()
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._playback_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read(), name="tel-agent-audiosocket-read")
        self._playback_task = asyncio.create_task(
            self._playback(), name="tel-agent-audiosocket-playback"
        )

    def _signal_inbound_end(self) -> None:
        try:
            self.inbound_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self.inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.inbound_queue.put_nowait(None)

    async def _read(self) -> None:
        try:
            while True:
                frame_type, payload = await read_frame(self.reader)
                if frame_type == TERMINATE:
                    break
                if frame_type == ERROR_FRAME:
                    raise RuntimeError(f"Asterisk AudioSocket error: {payload.hex()}")
                if frame_type == SLIN8_FRAME and payload:
                    await self.inbound_queue.put(payload)
                elif frame_type == DTMF_FRAME and payload:
                    await self.dtmf.put(payload.decode("ascii", "ignore")[:1])
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.closed.set()
            self._signal_inbound_end()

    async def _playback(self) -> None:
        loop = asyncio.get_running_loop()
        next_frame_at = loop.time()
        try:
            while True:
                frame = await self.playback_queue.get()
                try:
                    if frame is None:
                        return
                    delay = next_frame_at - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    async with self._write_lock:
                        self.writer.write(encode_frame(SLIN8_FRAME, frame))
                        await self.writer.drain()
                    next_frame_at = max(next_frame_at + FRAME_SECONDS, loop.time())
                finally:
                    self.playback_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("AudioSocket playback failed")
            self.closed.set()
            self._signal_inbound_end()
        finally:
            while True:
                try:
                    self.playback_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self.playback_queue.task_done()

    async def inbound(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.inbound_queue.get()
            if chunk is None:
                return
            yield chunk

    async def play(self, ulaw: bytes) -> None:
        pcm = mulaw_to_slin8(ulaw)
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset : offset + FRAME_BYTES]
            if len(frame) < FRAME_BYTES:
                frame += b"\0" * (FRAME_BYTES - len(frame))
            await self.playback_queue.put(frame)

    async def flush(self) -> None:
        while True:
            try:
                frame = self.playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.playback_queue.task_done()
                if frame is None:
                    await self.playback_queue.put(None)
                    break

    async def drain(self) -> None:
        await self.playback_queue.join()

    async def close(self) -> None:
        await self.flush()
        await self.playback_queue.put(None)
        tasks = [task for task in (self._reader_task, self._playback_task) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class AudioSocketSink:
    def __init__(self, room: AudioSocketRoom) -> None:
        self.room = room

    async def write(self, chunk: bytes) -> None:
        await self.room.play(chunk)

    async def flush(self) -> None:
        await self.room.flush()


def _bounded_env_number(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _voice_llm_payload(
    *,
    model: str,
    base_url: str,
    messages: list[Any],
    tools: Sequence[Any] | None,
) -> dict[str, Any]:
    """Build the low-latency payload missing from Tel-Agent's generic provider."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": _bounded_env_number("GROQ_LLM_TEMPERATURE", 0.30, 0.01, 2.0),
        "top_p": _bounded_env_number("GROQ_LLM_TOP_P", 0.70, 0.01, 1.0),
        "max_tokens": int(
            _bounded_env_number("GROQ_LLM_MAX_TOKENS", 160, 32, 1_024)
        ),
    }
    if tools:
        payload["tools"] = [tool.as_schema() for tool in tools]

    host = (urlparse(base_url).hostname or "").lower()
    is_groq = host == "api.groq.com" or host.endswith(".groq.com")
    if is_groq and "qwen3" in model.lower():
        # Groq otherwise defaults Qwen to a reasoning mode that can add latency and
        # expose thinking text to a caller. These controls match Floodman's proven
        # AVA profile while leaving non-Groq OpenAI-compatible endpoints untouched.
        payload["reasoning_effort"] = "none"
        payload["reasoning_format"] = "hidden"
    return payload


@dataclass(slots=True)
class RuntimeDependencies:
    llm: Any
    stt: Any
    tts: Any
    tool_type: Any
    text_delta_type: Any
    tool_call_type: Any
    final_type: Any
    partial_type: Any
    speak: Any


def load_runtime_dependencies() -> RuntimeDependencies:
    """Import only Tel-Agent's core; the optional LiveKit module is never imported."""
    import websockets

    from agent.config import LlmSettings, SttSettings, TtsSettings
    from agent.providers.llm.base import TextDelta, ToolCall
    from agent.providers.llm.openai_compatible import OpenAICompatibleLLM
    from agent.providers.stt.base import Final, Partial
    from agent.providers.stt.deepgram import DeepgramSTT, _pump
    from agent.providers.tts.elevenlabs import ElevenLabsTTS
    from agent.session.turn import speak
    from agent.tools import Tool

    llm_key = os.getenv("GROQ_API_KEY", "").strip()
    deepgram_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if not all((llm_key, deepgram_key, elevenlabs_key, voice_id)):
        raise RuntimeError("Tel-Agent requires configured Groq, Deepgram, and ElevenLabs credentials")

    stt_configuration = SttSettings(
        api_key=deepgram_key,
        model=os.getenv("TEL_AGENT_DEEPGRAM_MODEL", "nova-3").strip(),
        language=os.getenv("STT_LANGUAGE", "en-US").strip(),
        base_url=os.getenv("DEEPGRAM_BASE_URL", "wss://api.deepgram.com")
        .strip()
        .rstrip("/"),
        encoding="linear16",
        sample_rate=8_000,
    )
    try:
        endpointing_ms = int(os.getenv("TEL_AGENT_ENDPOINTING_MS", "450"))
    except ValueError:
        endpointing_ms = 450
    endpointing_ms = max(300, min(1_000, endpointing_ms))

    class UtteranceDeepgramSTT:
        """Tel-Agent Deepgram stream with complete-utterance endpoint semantics."""

        def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Any]:
            base_url = DeepgramSTT(stt_configuration)._url()
            url = f"{base_url}&endpointing={endpointing_ms}&smart_format=true"

            async def generate() -> AsyncIterator[Any]:
                accumulator = DeepgramUtteranceAccumulator(stt_configuration.language)
                headers = {"Authorization": f"Token {stt_configuration.api_key}"}
                async with websockets.connect(url, additional_headers=headers) as socket:
                    sender = asyncio.ensure_future(_pump(socket, audio))
                    try:
                        async for raw in socket:
                            utterance = accumulator.feed(raw)
                            if utterance is None:
                                continue
                            if utterance.kind == "final":
                                yield Final(
                                    text=utterance.text,
                                    confidence=utterance.confidence,
                                    language=utterance.language,
                                )
                            else:
                                yield Partial(text=utterance.text)
                        tail = accumulator.finish()
                        if tail is not None:
                            yield Final(
                                text=tail.text,
                                confidence=tail.confidence,
                                language=tail.language,
                            )
                    finally:
                        sender.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await sender

            return generate()

    class FloodmanVoiceLLM(OpenAICompatibleLLM):
        """Tel-Agent streaming parser with Floodman's voice-specific Groq controls."""

        async def stream(self, messages: list[Any], tools: Sequence[Any] | None = None):
            payload = _voice_llm_payload(
                model=self._settings.model,
                base_url=self._settings.base_url,
                messages=messages,
                tools=tools,
            )
            headers = {"Authorization": f"Bearer {self._settings.api_key}"}
            url = f"{self._settings.base_url}/chat/completions"
            if self._client is not None:
                async for event in self._read(self._client, url, payload, headers):
                    yield event
                return

            timeout_seconds = _bounded_env_number(
                "GROQ_LLM_TIMEOUT_SECONDS", 8.0, 2.0, 30.0
            )
            timeout = httpx.Timeout(
                connect=min(5.0, timeout_seconds),
                read=timeout_seconds,
                write=5.0,
                pool=5.0,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async for event in self._read(client, url, payload, headers):
                    yield event

    return RuntimeDependencies(
        llm=FloodmanVoiceLLM(
            LlmSettings(
                provider="openai",
                model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.6-27b").strip(),
                api_key=llm_key,
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
                .strip()
                .rstrip("/"),
            )
        ),
        stt=UtteranceDeepgramSTT(),
        tts=ElevenLabsTTS(
            TtsSettings(
                api_key=elevenlabs_key,
                voice_id=voice_id,
                model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5").strip(),
                output_format="ulaw_8000",
                base_url=os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
                .strip()
                .rstrip("/"),
            )
        ),
        tool_type=Tool,
        text_delta_type=TextDelta,
        tool_call_type=ToolCall,
        final_type=Final,
        partial_type=Partial,
        speak=speak,
    )


def _special_tool_specs() -> dict[str, dict[str, Any]]:
    return {
        "hangup_call": {
            "description": "End the call only after the conversation has reached a safe conclusion.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "additionalProperties": False,
            },
            "timeout": 3.0,
        },
        "transfer": {
            "description": (
                "Transfer to a configured Floodman team after the caller asks "
                "or policy requires it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "enum": ["live_reception", "emergency", "billing", "estimating"],
                    }
                },
                "required": ["destination"],
                "additionalProperties": False,
            },
            "timeout": 5.0,
        },
    }


def build_tools(
    definitions: dict[str, dict[str, Any]],
    context: SessionContext,
    control: TurnControl,
    client: ControlClient,
    tool_type: Any,
) -> list[Any]:
    specs = {**definitions, **_special_tool_specs()}
    tools: list[Any] = []
    for name in context.agent.tools:
        spec = specs.get(name)
        if not spec:
            continue

        async def run(arguments: dict[str, Any], *, _name: str = name, _spec: dict[str, Any] = spec) -> str:
            return await client.run_tool(
                context, control, _name, arguments, float(_spec.get("timeout") or 6.0)
            )

        tools.append(
            tool_type(
                name=name,
                description=str(spec["description"]),
                parameters=dict(spec["parameters"]),
                run=run,
            )
        )
    return tools


async def reply_with_prompt(
    text: str,
    *,
    system_prompt: str,
    provider: Any,
    history: Sequence[dict[str, Any]],
    tools: Sequence[Any],
    control: TurnControl,
    text_delta_type: Any,
) -> AsyncIterator[str]:
    """Tel-Agent's streaming tool loop with Floodman's per-agent prompt."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    by_name = {tool.name: tool for tool in tools}

    for _ in range(MAX_TOOL_ROUNDS):
        spoken: list[str] = []
        calls: list[Any] = []
        async for event in provider.stream(messages, tools):
            if isinstance(event, text_delta_type):
                spoken.append(event.text)
                yield event.text
            else:
                calls.append(event)
        if not calls:
            return
        messages.append(
            {
                "role": "assistant",
                "content": "".join(spoken) if spoken else None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ],
            }
        )
        for call in calls:
            tool = by_name.get(call.name)
            if tool is None:
                result = json.dumps({"ok": False, "error": "unknown_tool"})
            else:
                try:
                    arguments = json.loads(call.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    result = await tool.run(arguments)
                except Exception as exc:
                    LOGGER.warning("Tool %s failed: %s", call.name, type(exc).__name__)
                    result = json.dumps(
                        {
                            "ok": False,
                            "error": "tool_unavailable",
                            "safe_message": "That action is unavailable. Offer a human callback.",
                        }
                    )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )
        if control.direct_message:
            yield control.direct_message
            control.direct_message = ""
            return
        if control.end_requested:
            return
    LOGGER.warning("Model exceeded the maximum tool rounds")


async def _single_text(text: str) -> AsyncIterator[str]:
    yield text


class CallSession:
    def __init__(
        self,
        context: SessionContext,
        room: AudioSocketRoom,
        control_client: ControlClient,
        runtime: RuntimeDependencies,
        definitions: dict[str, dict[str, Any]],
    ) -> None:
        self.context = context
        self.room = room
        self.client = control_client
        self.runtime = runtime
        self.control = TurnControl()
        self.tools = build_tools(
            definitions, context, self.control, control_client, runtime.tool_type
        )
        self.history: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        self.active_turn: asyncio.Task[None] | None = None
        self.failure: BaseException | None = None
        self.failure_event = asyncio.Event()
        self.end_event = asyncio.Event()

    async def append_turn(
        self,
        role: str,
        text: str,
        *,
        confidence: float | None = None,
        language: str | None = None,
        interrupted: bool = False,
    ) -> None:
        clean = re.sub(r"\s+", " ", text).strip()[:100_000]
        if not clean:
            return
        self.control.sequence += 1
        turn = {
            "sequence": self.control.sequence,
            "role": role,
            "content": clean,
            "confidence": confidence,
            "language": language,
            "interrupted": interrupted,
        }
        self.transcript.append(turn)
        self.history.append({"role": role, "content": clean})
        await self.client.persist_turn(
            self.context,
            sequence=self.control.sequence,
            role=role,
            text=clean,
            confidence=confidence,
            language=language,
            interrupted=interrupted,
        )

    def _track(self, task: asyncio.Task[None]) -> None:
        self.active_turn = task

        def completed(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.failure = error
                self.failure_event.set()
            elif self.control.end_requested:
                self.end_event.set()

        task.add_done_callback(completed)

    async def _speak_stream(self, stream: AsyncIterator[str], since: float | None = None) -> None:
        sink = AudioSocketSink(self.room)
        result = await self.runtime.speak(
            stream,
            tts=self.runtime.tts,
            sink=sink,
            should_stop=lambda: self.control.stop_speaking or self.room.closed.is_set(),
            since=since,
        )
        await self.room.drain()
        await self.append_turn("assistant", result.text, interrupted=result.interrupted)

    async def _answer(self, text: str, since: float) -> None:
        history = list(self.history[:-1])
        stream = reply_with_prompt(
            text,
            system_prompt=self.context.agent.prompt,
            provider=self.runtime.llm,
            history=history,
            tools=self.tools,
            control=self.control,
            text_delta_type=self.runtime.text_delta_type,
        )
        await self._speak_stream(stream, since)

    async def _stop_active_turn(self) -> None:
        if not self.active_turn or self.active_turn.done():
            return
        self.control.stop_speaking = True
        await self.room.flush()
        try:
            await asyncio.wait_for(self.active_turn, timeout=3.0)
        except asyncio.TimeoutError:
            self.active_turn.cancel()
            await asyncio.gather(self.active_turn, return_exceptions=True)

    async def consume_transcripts(self) -> None:
        async for event in self.runtime.stt.stream(self.room.inbound()):
            if isinstance(event, self.runtime.partial_type):
                if str(event.text).strip() and self.active_turn and not self.active_turn.done():
                    self.control.stop_speaking = True
                    await self.room.flush()
                continue
            if not isinstance(event, self.runtime.final_type):
                continue
            text = str(event.text).strip()
            if not text:
                continue
            ended = time.perf_counter()
            await self._stop_active_turn()
            await self.append_turn(
                "user", text, confidence=event.confidence, language=event.language
            )
            self.control.stop_speaking = False
            self.control.direct_message = ""
            task = asyncio.create_task(self._answer(text, ended), name="tel-agent-answer")
            self._track(task)
            if self.control.end_requested:
                break

    async def run(self) -> None:
        if self.context.opening_transcript:
            await self.append_turn("user", self.context.opening_transcript)
            self.control.stop_speaking = False
            self._track(
                asyncio.create_task(
                    self._answer(self.context.opening_transcript, time.perf_counter()),
                    name="tel-agent-opening-answer",
                )
            )
        else:
            self._track(
                asyncio.create_task(
                    self._speak_stream(_single_text(self.context.agent.greeting)),
                    name="tel-agent-greeting",
                )
            )

        consumer = asyncio.create_task(
            self.consume_transcripts(), name="tel-agent-transcript-consumer"
        )
        failed = asyncio.create_task(self.failure_event.wait(), name="tel-agent-failure-watch")
        ended = asyncio.create_task(self.end_event.wait(), name="tel-agent-end-watch")

        async def hangup_grace() -> None:
            await self.room.closed.wait()
            # Let Deepgram process CloseStream and return its last final transcript.
            await asyncio.sleep(3.0)

        closed = asyncio.create_task(hangup_grace(), name="tel-agent-hangup-grace")
        done, pending = await asyncio.wait(
            {consumer, failed, ended, closed}, return_when=asyncio.FIRST_COMPLETED
        )
        if failed in done and self.failure is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            raise self.failure
        if consumer in done:
            error = consumer.exception()
            if error:
                raise error
            if not self.room.closed.is_set() and not self.control.end_requested:
                raise RuntimeError("Deepgram transcript stream ended before the call")
        if self.active_turn and not self.active_turn.done():
            await self._stop_active_turn()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class TelAgentAudioSocketServer:
    def __init__(self, host: str, port: int, config_path: Path, ready_file: Path) -> None:
        self.host = host
        self.port = port
        self.config_path = config_path
        self.ready_file = ready_file
        self.runtime = load_runtime_dependencies()
        self.definitions = load_tool_definitions(config_path)
        self.control = ControlClient(
            os.getenv("FLOODMAN_INTERNAL_URL", "http://127.0.0.1:9000"),
            os.getenv("INTERNAL_TOKEN", ""),
        )
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Tel-Agent AudioSocket must bind to loopback")
        await self.control.ready()
        self.server = await asyncio.start_server(self.handle, self.host, self.port)
        self.ready_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ready_file.with_suffix(".tmp")
        temporary.write_text(f"pid={os.getpid()}\nport={self.port}\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.ready_file)
        LOGGER.info("Tel-Agent AudioSocket ready on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        self.ready_file.unlink(missing_ok=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await self.control.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        room: AudioSocketRoom | None = None
        context: SessionContext | None = None
        session: CallSession | None = None
        started = time.monotonic()
        outcome = "caller_hangup"
        error_type = ""
        try:
            frame_type, payload = await asyncio.wait_for(read_frame(reader), timeout=3.0)
            if frame_type != UUID_FRAME or len(payload) != 16:
                raise ValueError("AudioSocket connection must start with a 16-byte UUID")
            gate_uuid = str(uuid.UUID(bytes=payload))
            context = await self.control.session(gate_uuid)
            room = AudioSocketRoom(reader, writer)
            room.start()
            session = CallSession(context, room, self.control, self.runtime, self.definitions)
            await session.run()
            if session.control.transferred:
                outcome = "transferred"
            elif session.control.end_requested:
                outcome = "agent_hangup"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            outcome = "provider_failure"
            LOGGER.exception("Tel-Agent call failed: %s", error_type)
            if context and await self.control.fallback_on_failure(context, error_type):
                outcome = "provider_failure_ava_fallback"
            elif context and await self.control.transfer_on_failure(context):
                outcome = "provider_failure_transferred"
        finally:
            # AVA owns final completion after a successful runtime handoff. Sending a
            # competing completion here would race it and could truncate the transcript.
            if context and outcome != "provider_failure_ava_fallback":
                try:
                    await self.control.completed(
                        context,
                        session.transcript if session else [],
                        duration=time.monotonic() - started,
                        outcome=outcome,
                        error_type=error_type,
                    )
                except Exception:
                    LOGGER.exception("Could not persist Tel-Agent call completion")
            if room:
                await room.close()
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


async def async_main(args: argparse.Namespace) -> None:
    ready_file = Path(args.ready_file)
    ready_file.unlink(missing_ok=True)
    server = TelAgentAudioSocketServer(args.host, args.port, Path(args.config), ready_file)
    try:
        await server.start()
        assert server.server is not None
        await server.server.serve_forever()
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Floodman Tel-Agent AudioSocket service")
    parser.add_argument("--host", default=os.getenv("TEL_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TEL_AGENT_PORT", "9020")))
    parser.add_argument(
        "--config",
        default=os.getenv(
            "TEL_AGENT_TOOL_CONFIG",
            str(Path(os.getenv("CONFIG_DIR", "/home/container/data/config")) / "ava/ai-agent.local.yaml"),
        ),
    )
    parser.add_argument(
        "--ready-file",
        default=str(Path(os.getenv("DATA_DIR", "/home/container/data")) / "runtime/tel-agent-ready"),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
