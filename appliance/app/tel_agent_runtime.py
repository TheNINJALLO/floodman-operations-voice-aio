# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tel-Agent providers for Floodman's deterministic Asterisk call controller.

LiveKit is deliberately absent. Asterisk remains the private media transport and
Floodman's VoiceCore remains the authority for intake, compliance, and transfers.
"""

from __future__ import annotations

import logging
import os
import struct
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.llm import LocalLLM
from app.stt import LocalSTT
from app.tts import LocalTTS

logger = logging.getLogger(__name__)


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
        self._load_provider()
        pieces: list[str] = []
        try:
            async for event in self._provider.stream_configured(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                if isinstance(event, self._text_delta_type):
                    pieces.append(event.text)
            answer = "".join(pieces).strip()
            if answer:
                return answer
            raise RuntimeError("Tel-Agent model returned an empty reply")
        except Exception as exc:
            logger.warning(
                "Tel-Agent model unavailable; using local completion fallback: %s",
                type(exc).__name__,
            )
            return await super()._chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )


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

    async def synthesize(self, text: str, *, speed: float | None = None) -> bytes:
        return b"".join([part async for part in self.stream_pcm(text, speed=speed)])

    async def available_voices(self) -> list[str]:
        await LocalTTS.start(self)
        return await LocalTTS.available_voices(self)
