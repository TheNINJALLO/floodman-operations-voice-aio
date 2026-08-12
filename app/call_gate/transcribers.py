from __future__ import annotations

import asyncio
import io
import logging
import wave
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class Transcriber(ABC):
    name = "base"

    @abstractmethod
    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        raise NotImplementedError


class MetadataTranscriber(Transcriber):
    name = "metadata"

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return ""


class OpenAICompatibleTranscriber(Transcriber):
    name = "openai-compatible"

    def __init__(self, settings: Settings):
        if not settings.gate_stt_url:
            raise ValueError("GATE_STT_URL is required for openai-compatible transcription")
        self.url = settings.gate_stt_url
        self.api_key = settings.gate_stt_api_key
        self.model = settings.gate_stt_model
        self.language = settings.gate_stt_language
        self.timeout = max(3.0, settings.gate_max_seconds)

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        wav = pcm_to_wav(pcm, sample_rate)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data: dict[str, Any] = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.url,
                headers=headers,
                data=data,
                files={"file": ("gate.wav", wav, "audio/wav")},
            )
            response.raise_for_status()
            payload = response.json()
        return str(payload.get("text") or payload.get("transcript") or "").strip()


class DeepgramTranscriber(Transcriber):
    name = "deepgram"

    def __init__(self, settings: Settings):
        if not settings.gate_stt_api_key:
            raise ValueError("GATE_STT_API_KEY is required for Deepgram transcription")
        self.api_key = settings.gate_stt_api_key
        configured_model = (settings.gate_stt_model or "").strip().lower()
        local_model_names = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "whisper-1"}
        self.model = "nova-3" if configured_model in local_model_names or not configured_model else configured_model
        self.language = settings.gate_stt_language
        self.timeout = max(3.0, settings.gate_max_seconds)

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        wav = pcm_to_wav(pcm, sample_rate)
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "false",
        }
        if self.language:
            params["language"] = self.language
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=params,
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "audio/wav"},
                content=wav,
            )
            response.raise_for_status()
            payload = response.json()
        try:
            return str(payload["results"]["channels"][0]["alternatives"][0]["transcript"]).strip()
        except (KeyError, IndexError, TypeError):
            return ""


class FasterWhisperTranscriber(Transcriber):
    name = "faster-whisper"

    def __init__(self, settings: Settings):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Install the project with the local-stt extra."
            ) from exc
        self._model = WhisperModel(
            settings.faster_whisper_model,
            device=settings.faster_whisper_device,
            compute_type=settings.faster_whisper_compute_type,
        )
        self.language = settings.gate_stt_language or "en"

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str:
        import tempfile

        wav = pcm_to_wav(pcm, sample_rate)
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            handle.write(wav)
            handle.flush()
            segments, _ = self._model.transcribe(
                handle.name,
                language=self.language,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)


def build_transcriber(settings: Settings) -> Transcriber:
    choice = settings.gate_transcriber.strip().lower().replace("_", "-")
    if choice in {"", "metadata", "off", "none"}:
        return MetadataTranscriber()
    if choice in {"deepgram", "dg"}:
        return DeepgramTranscriber(settings)
    if choice in {"openai", "openai-compatible", "whisper-api", "groq"}:
        return OpenAICompatibleTranscriber(settings)
    if choice in {"faster-whisper", "local", "whisper"}:
        return FasterWhisperTranscriber(settings)
    raise ValueError(f"Unsupported GATE_TRANSCRIBER: {settings.gate_transcriber}")
