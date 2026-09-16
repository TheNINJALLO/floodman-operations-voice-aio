# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tel-Agent providers for Floodman's deterministic Asterisk call controller.

LiveKit is deliberately absent. Asterisk remains the private media transport and
Floodman's VoiceCore remains the authority for intake, compliance, and transfers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import struct
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import websockets

from app.config import Settings
from app.llm import LocalLLM
from app.stt import LocalSTT
from app.tts import LocalTTS

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelAgentTranscriptTurn:
    """One complete caller turn assembled from stable streaming STT segments."""

    text: str
    confidence: float | None = None
    language: str = ""


@dataclass(slots=True)
class DeepgramTurnAssembler:
    """Accumulate every stable segment until Deepgram marks the turn complete."""

    segments: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    language: str = ""

    def accept(self, message: dict[str, Any]) -> TelAgentTranscriptTurn | None:
        kind = str(message.get("type") or "")
        if kind == "Results":
            channel = message.get("channel") or {}
            alternatives = channel.get("alternatives") or []
            if not alternatives:
                return None
            alternative = alternatives[0] or {}
            text = str(alternative.get("transcript") or "").strip()
            if bool(message.get("is_final")) and text:
                self.segments.append(text)
                confidence = alternative.get("confidence")
                if isinstance(confidence, int | float):
                    self.confidences.append(float(confidence))
                self.language = str(channel.get("detected_language") or self.language)
            if bool(message.get("speech_final")):
                return self.finish()
            return None
        if kind == "UtteranceEnd":
            return self.finish()
        return None

    def finish(self) -> TelAgentTranscriptTurn | None:
        text = " ".join(part for part in self.segments if part).strip()
        if not text:
            return None
        confidence = (
            sum(self.confidences) / len(self.confidences)
            if self.confidences
            else None
        )
        turn = TelAgentTranscriptTurn(
            text=text,
            confidence=confidence,
            language=self.language,
        )
        self.segments.clear()
        self.confidences.clear()
        self.language = ""
        return turn


def mulaw_to_slin8(payload: bytes) -> bytes:
    """Decode G.711 mu-law into Asterisk signed-linear 8 kHz PCM."""
    output = bytearray(len(payload) * 2)
    offset = 0
    for encoded in payload:
        value = (~encoded) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
        if sign:
            sample = -sample
        struct.pack_into("<h", output, offset, max(-32_768, min(32_767, sample)))
        offset += 2
    return bytes(output)


def _bounded_number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _provider_payload(
    *,
    model: str,
    base_url: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    tools: Sequence[Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": max(0.01, min(2.0, float(temperature))),
        "max_tokens": max(32, min(1_024, int(max_tokens))),
    }
    if tools:
        payload["tools"] = [tool.as_schema() for tool in tools]
    host = (urlparse(base_url).hostname or "").lower()
    if (host == "api.groq.com" or host.endswith(".groq.com")) and "qwen3" in model.lower():
        payload["reasoning_effort"] = "none"
        payload["reasoning_format"] = "hidden"
        payload["top_p"] = _bounded_number("GROQ_LLM_TOP_P", 0.70, 0.01, 1.0)
    elif host in {"127.0.0.1", "::1", "localhost"}:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


class TelAgentLLM(LocalLLM):
    """Use Tel-Agent's streaming OpenAI-compatible parser with local fail-open."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._provider: Any = None
        self._text_delta_type: Any = None
        self._cloud_key = os.getenv("GROQ_API_KEY", "").strip()
        self._base_url = (
            os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
            if self._cloud_key
            else settings.llama_base_url
        )
        self._model = (
            os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.6-27b").strip()
            if self._cloud_key
            else settings.llama_model_alias
        )

    @property
    def provider_name(self) -> str:
        return "tel-agent-groq" if self._cloud_key else "tel-agent-local-llama"

    def _load_provider(self) -> None:
        if self._provider is not None:
            return
        from agent.config import LlmSettings
        from agent.providers.llm.base import TextDelta
        from agent.providers.llm.openai_compatible import OpenAICompatibleLLM

        base_url = self._base_url
        cloud = bool(self._cloud_key)
        outer = self

        class FloodmanVoiceLLM(OpenAICompatibleLLM):
            async def stream_configured(
                self,
                messages: list[dict[str, str]],
                *,
                max_tokens: int,
                temperature: float,
                tools: Sequence[Any] | None = None,
            ) -> AsyncIterator[Any]:
                payload = _provider_payload(
                    model=self._settings.model,
                    base_url=self._settings.base_url,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                )
                headers = {"Authorization": f"Bearer {self._settings.api_key}"}
                url = f"{self._settings.base_url}/chat/completions"
                timeout_seconds = (
                    _bounded_number("GROQ_LLM_TIMEOUT_SECONDS", 8.0, 2.0, 30.0)
                    if cloud
                    else max(2.0, min(30.0, outer.settings.llama_timeout_seconds))
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

        api_key = self._cloud_key or self.settings.llama_api_key
        self._provider = FloodmanVoiceLLM(
            LlmSettings(
                provider="openai",
                model=self._model,
                api_key=api_key,
                base_url=base_url,
            )
        )
        self._text_delta_type = TextDelta

    async def health(self) -> bool:
        self._load_provider()
        if self._cloud_key:
            try:
                headers = {"Authorization": f"Bearer {self._cloud_key}"}
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"{self._base_url}/models", headers=headers)
                if response.status_code < 400:
                    return True
                logger.warning("Tel-Agent Groq health returned status %s", response.status_code)
            except httpx.HTTPError as exc:
                logger.warning("Tel-Agent Groq health unavailable: %s", type(exc).__name__)
        return await super().health()

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
        temperature: float = 0.1,
    ) -> str:
        pieces: list[str] = []
        async for piece in self.stream_text(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            pieces.append(piece)
        return "".join(pieces).strip()

    async def stream_text(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        """Yield model deltas so Tel-Agent can speak before generation finishes."""

        self._load_provider()
        emitted = False
        try:
            async for event in self._provider.stream_configured(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                if isinstance(event, self._text_delta_type):
                    text = str(event.text or "")
                    if text:
                        emitted = True
                        yield text
            if emitted:
                return
            raise RuntimeError("Tel-Agent model returned an empty reply")
        except Exception as exc:
            if emitted:
                logger.warning(
                    "Tel-Agent model stream ended after partial output: %s",
                    type(exc).__name__,
                )
                return
            logger.warning(
                "Tel-Agent model unavailable; using local completion fallback: %s",
                type(exc).__name__,
            )
            answer = await super()._chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if answer:
                yield answer

    async def stream_spoken_reply(
        self,
        *,
        caller_text: str,
        required_reply: str,
        state: dict[str, Any],
        history: Sequence[dict[str, str]],
    ) -> AsyncIterator[str]:
        """Naturalize wording without allowing the model to change policy decisions."""

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Alex, Floodman's automated phone assistant. The deterministic "
                    "policy engine has already decided the facts, action, and next question. "
                    "Rewrite only the wording so it sounds like a warm, concise phone "
                    "conversation. Preserve every fact and the same next question. Use one or "
                    "two short sentences, contractions when natural, no markdown, and no lists. "
                    "Never add prices, promises, availability, diagnoses, insurance decisions, "
                    "verification claims, or actions that are absent from the required reply."
                ),
            }
        ]
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in history[-20:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Caller just said: {caller_text}\n"
                    f"Current intake stage: {str(state.get('stage') or '')}\n"
                    f"Required response: {required_reply}"
                ),
            }
        )
        async for piece in self.stream_text(
            messages,
            max_tokens=140,
            temperature=0.25,
        ):
            yield piece


class TelAgentSTT(LocalSTT):
    """Use Tel-Agent Deepgram when configured, otherwise preserve local Whisper."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._cloud_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        self._provider: Any = None
        self._final_type: Any = None

    @property
    def provider_name(self) -> str:
        return "tel-agent-deepgram" if self._cloud_key else "tel-agent-local-whisper"

    @property
    def ready(self) -> bool:
        return self._provider is not None if self._cloud_key else self._model is not None

    @property
    def streaming(self) -> bool:
        return bool(self._cloud_key)

    async def start(self) -> None:
        if not self._cloud_key:
            await super().start()
            return
        if self._provider is not None:
            return
        from agent.config import SttSettings
        from agent.providers.stt.base import Final
        from agent.providers.stt.deepgram import DeepgramSTT

        self._provider = DeepgramSTT(
            SttSettings(
                api_key=self._cloud_key,
                model=os.getenv("TEL_AGENT_DEEPGRAM_MODEL", "nova-3").strip(),
                language=os.getenv("STT_LANGUAGE", "en-US").strip(),
                base_url=os.getenv("DEEPGRAM_BASE_URL", "wss://api.deepgram.com")
                .strip()
                .rstrip("/"),
                encoding="linear16",
                sample_rate=8_000,
            )
        )
        self._final_type = Final
        logger.info("Tel-Agent Deepgram STT configured")

    def _stream_url(self, sample_rate: int) -> str:
        base_url = os.getenv("DEEPGRAM_BASE_URL", "wss://api.deepgram.com").strip().rstrip("/")
        params = {
            "channels": "1",
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "model": os.getenv("TEL_AGENT_DEEPGRAM_MODEL", "nova-3").strip(),
            "language": os.getenv("STT_LANGUAGE", "en-US").strip(),
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "vad_events": "true",
            "endpointing": str(
                int(_bounded_number("TEL_AGENT_ENDPOINTING_MS", 450, 100, 1500))
            ),
            "utterance_end_ms": str(
                int(_bounded_number("TEL_AGENT_UTTERANCE_END_MS", 1000, 1000, 3000))
            ),
        }
        return f"{base_url}/v1/listen?{urlencode(params)}"

    async def stream_turns(
        self,
        audio: AsyncIterator[bytes],
        *,
        sample_rate: int = 8000,
    ) -> AsyncIterator[TelAgentTranscriptTurn]:
        """Keep one Deepgram stream open and emit complete, lossless caller turns."""

        if not self._cloud_key:
            raise RuntimeError("Continuous Tel-Agent STT requires DEEPGRAM_API_KEY")
        headers = {"Authorization": f"Token {self._cloud_key}"}
        assembler = DeepgramTurnAssembler()
        async with websockets.connect(
            self._stream_url(sample_rate),
            additional_headers=headers,
            open_timeout=5,
            close_timeout=3,
            ping_interval=10,
            ping_timeout=10,
        ) as socket:
            sender = asyncio.create_task(self._pump_stream_audio(socket, audio))
            keepalive = asyncio.create_task(self._keep_stream_alive(socket))
            try:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    turn = assembler.accept(message)
                    if turn is not None:
                        yield turn
            finally:
                sender.cancel()
                keepalive.cancel()
                await asyncio.gather(sender, keepalive, return_exceptions=True)

    @staticmethod
    async def _pump_stream_audio(socket: Any, audio: AsyncIterator[bytes]) -> None:
        try:
            async for chunk in audio:
                if chunk:
                    await socket.send(chunk)
        except websockets.ConnectionClosed:
            return
        with contextlib.suppress(websockets.ConnectionClosed):
            await socket.send(json.dumps({"type": "CloseStream"}))

    @staticmethod
    async def _keep_stream_alive(socket: Any) -> None:
        """Keep continuous recognition open while the assistant is speaking."""

        while True:
            await asyncio.sleep(3)
            try:
                await socket.send(json.dumps({"type": "KeepAlive"}))
            except websockets.ConnectionClosed:
                return

    async def transcribe(
        self,
        pcm16le: bytes,
        sample_rate: int = 8000,
        *,
        contact: bool = False,
    ) -> str:
        if not self._cloud_key:
            return await super().transcribe(pcm16le, sample_rate, contact=contact)
        await self.start()

        async def audio() -> AsyncIterator[bytes]:
            frame_bytes = max(320, int(sample_rate * 2 / 10))
            for offset in range(0, len(pcm16le), frame_bytes):
                yield pcm16le[offset : offset + frame_bytes]

        try:
            segments: list[str] = []
            async for event in self._provider.stream(audio()):
                if isinstance(event, self._final_type):
                    text = str(event.text or "").strip()
                    if text:
                        segments.append(text)
            return " ".join(segments).strip()
        except Exception as exc:
            logger.warning(
                "Tel-Agent Deepgram unavailable; using local Whisper fallback: %s",
                type(exc).__name__,
            )
            await LocalSTT.start(self)
            return await LocalSTT.transcribe(self, pcm16le, sample_rate, contact=contact)

    async def transcribe_local(
        self,
        pcm16le: bytes,
        sample_rate: int = 8000,
        *,
        contact: bool = False,
    ) -> str:
        """Bypass a failed cloud stream and use the private Whisper fallback."""

        await LocalSTT.start(self)
        return await LocalSTT.transcribe(self, pcm16le, sample_rate, contact=contact)


class TelAgentTTS(LocalTTS):
    """Use Tel-Agent ElevenLabs streaming TTS with local Kokoro fail-open."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._cloud_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        self._voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
        self._provider: Any = None

    @property
    def provider_name(self) -> str:
        return (
            "tel-agent-elevenlabs"
            if self._cloud_key and self._voice_id
            else "tel-agent-local-kokoro"
        )

    @property
    def streaming(self) -> bool:
        return self._provider is not None

    @property
    def ready(self) -> bool:
        return self._provider is not None or self._kokoro is not None

    async def start(self) -> None:
        if not self._cloud_key and not self._voice_id:
            await super().start()
            return
        if not self._cloud_key or not self._voice_id:
            logger.warning("ElevenLabs configuration is incomplete; using local Kokoro")
            await LocalTTS.start(self)
            return
        if self._provider is not None:
            return
        from agent.config import TtsSettings
        from agent.providers.tts.elevenlabs import ElevenLabsTTS

        self._provider = ElevenLabsTTS(
            TtsSettings(
                api_key=self._cloud_key,
                voice_id=self._voice_id,
                model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5").strip(),
                output_format="ulaw_8000",
                base_url=os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
                .strip()
                .rstrip("/"),
            )
        )
        logger.info("Tel-Agent ElevenLabs TTS configured")

    async def warm(self, phrases: tuple[str, ...]) -> None:
        await self.start()
        if self._provider is None:
            await LocalTTS.warm(self, phrases)

    async def stream_pcm(
        self, text: str, *, speed: float | None = None
    ) -> AsyncIterator[bytes]:
        del speed
        await self.start()
        if self._provider is not None:
            emitted = False
            try:
                async for chunk in self._provider.stream(text):
                    emitted = True
                    yield mulaw_to_slin8(chunk)
                return
            except Exception as exc:
                if emitted:
                    raise
                logger.warning(
                    "Tel-Agent ElevenLabs unavailable; using local Kokoro: %s",
                    type(exc).__name__,
                )
        await LocalTTS.start(self)
        pcm = await LocalTTS.synthesize(self, text)
        if pcm:
            yield pcm

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """Tel-Agent TTSProvider surface used by agent.session.turn.speak."""

        return self.stream_pcm(text)

    async def synthesize(self, text: str, *, speed: float | None = None) -> bytes:
        return b"".join([part async for part in self.stream_pcm(text, speed=speed)])

    async def available_voices(self) -> list[str]:
        await LocalTTS.start(self)
        return await LocalTTS.available_voices(self)
