from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from app.config import Settings

logger = logging.getLogger(__name__)


class LocalTTS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._kokoro = None
        self._lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()
        self.cache_dir = settings.cache_dir / "tts"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        async with self._lock:
            if self._kokoro is not None:
                return
            from kokoro_onnx import Kokoro
            self._kokoro = await asyncio.to_thread(
                Kokoro,
                str(self.settings.kokoro_model_path),
                str(self.settings.kokoro_voices_path),
            )
            logger.info("Kokoro ready with voice %s", self.settings.kokoro_voice)

    def _cache_path(self, text: str, voice: str | None = None, speed: float | None = None) -> Path:
        selected_voice = voice if voice is not None else self.settings.kokoro_voice
        selected_speed = speed if speed is not None else self.settings.kokoro_speed
        key = hashlib.sha256(f"{selected_voice}|{selected_speed}|{text}".encode()).hexdigest()
        return self.cache_dir / f"{key}.pcm"

    @staticmethod
    def _to_pcm(samples: np.ndarray, source_rate: int) -> bytes:
        samples = np.asarray(samples, dtype=np.float32)
        if source_rate != 8000:
            samples = resample_poly(samples, 8000, source_rate)
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype("<i2").tobytes()

    async def synthesize(self, text: str) -> bytes:
        text = str(text or "").strip()
        if not text:
            return b""
        voice = self.settings.kokoro_voice
        speed = self.settings.kokoro_speed
        path = self._cache_path(text, voice, speed)
        if self.settings.tts_cache_enabled and path.exists():
            return path.read_bytes()
        try:
            await self.start()
            assert self._kokoro is not None
            async with self._synthesis_lock:
                samples, rate = await asyncio.to_thread(
                    self._kokoro.create,
                    text,
                    voice=voice,
                    speed=speed,
                    lang=self._language(voice),
                )
            pcm = self._to_pcm(samples, int(rate))
        except Exception as exc:
            logger.exception("Kokoro failed, using eSpeak fallback: %s", exc)
            pcm = await asyncio.to_thread(self._espeak, text)
        if self.settings.tts_cache_enabled and pcm:
            path.write_bytes(pcm)
        return pcm

    @staticmethod
    def _language(voice: str) -> str:
        return "en-gb" if str(voice).startswith(("bf_", "bm_")) else "en-us"

    async def available_voices(self) -> list[str]:
        await self.start()
        assert self._kokoro is not None
        values = await asyncio.to_thread(self._kokoro.get_voices)
        return sorted({str(value) for value in values if str(value).startswith(("af_", "am_", "bf_", "bm_"))})

    async def preview(self, text: str, voice: str, speed: float) -> bytes:
        message = str(text or "").strip()
        if not message:
            raise ValueError("Enter a short preview phrase.")
        if len(message) > 240:
            raise ValueError("Preview text must be 240 characters or fewer.")
        selected_speed = self._validated_speed(speed)
        voices = await self.available_voices()
        if voice not in voices:
            raise ValueError("Select an installed English voice.")
        assert self._kokoro is not None
        async with self._synthesis_lock:
            samples, rate = await asyncio.to_thread(
                self._kokoro.create,
                message,
                voice=voice,
                speed=selected_speed,
                lang=self._language(voice),
            )
        return self._to_pcm(samples, int(rate))

    async def configure(self, voice: str, speed: float) -> tuple[str, float]:
        selected_speed = self._validated_speed(speed)
        if voice not in await self.available_voices():
            raise ValueError("Select an installed English voice.")
        value = {"voice": voice, "speed": selected_speed}
        path = self.settings.voice_settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self._synthesis_lock:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            self.settings.kokoro_voice = voice
            self.settings.kokoro_speed = selected_speed
        return voice, selected_speed

    @staticmethod
    def _validated_speed(value: float) -> float:
        try:
            speed = round(float(value), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError("Speaking speed must be a number.") from exc
        if not math.isfinite(speed) or not 0.75 <= speed <= 1.25:
            raise ValueError("Speaking speed must be between 0.75 and 1.25.")
        return speed

    def _espeak(self, text: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="floodman-tts-") as tmp:
            wav_path = Path(tmp) / "speech.wav"
            subprocess.run(
                ["espeak-ng", "-v", "en-us+f3", "-s", "165", "-w", str(wav_path), text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with wave.open(str(wav_path), "rb") as wav:
                rate = wav.getframerate()
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                frames = wav.readframes(wav.getnframes())
            if width != 2:
                raise RuntimeError("eSpeak fallback did not produce 16-bit PCM")
            values = np.frombuffer(frames, dtype="<i2").astype(np.float32)
            if channels > 1:
                values = values.reshape(-1, channels).mean(axis=1)
            values /= 32768.0
            return self._to_pcm(values, rate)

    async def warm(self, phrases: tuple[str, ...]) -> None:
        for phrase in phrases:
            try:
                await self.synthesize(phrase)
            except Exception:
                logger.exception("Could not pre-synthesize prompt")
