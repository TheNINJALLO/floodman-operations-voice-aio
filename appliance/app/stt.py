from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np

from app.config import Settings

logger = logging.getLogger(__name__)


class LocalSTT:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel
            model_root = self.settings.model_dir / "faster-whisper"
            model_root.mkdir(parents=True, exist_ok=True)
            local_model = model_root / f"faster-whisper-{self.settings.faster_whisper_model}"
            model_reference = str(local_model) if (local_model / "model.bin").exists() else self.settings.faster_whisper_model
            self._model = await asyncio.to_thread(
                WhisperModel,
                model_reference,
                device=self.settings.faster_whisper_device,
                compute_type=self.settings.faster_whisper_compute_type,
                cpu_threads=self.settings.faster_whisper_threads,
                download_root=str(model_root),
            )
            logger.info("Faster-Whisper ready: %s", self.settings.faster_whisper_model)

    async def transcribe(self, pcm16le: bytes, sample_rate: int = 8000, *, contact: bool = False) -> str:
        if not pcm16le:
            return ""
        await self.start()
        audio = np.frombuffer(pcm16le, dtype="<i2").astype(np.float32) / 32768.0
        if sample_rate != 16000:
            from scipy.signal import resample_poly
            audio = resample_poly(audio, 16000, sample_rate).astype(np.float32)

        def run(*, vad_filter: bool, beam_size: int, best_of: int) -> str:
            assert self._model is not None
            segments, _ = self._model.transcribe(
                audio,
                language="en",
                beam_size=beam_size,
                best_of=best_of,
                temperature=0.0,
                vad_filter=vad_filter,
                condition_on_previous_text=False,
                word_timestamps=False,
            )
            return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()

        if contact:
            # AudioSocket has already endpointed this clip with a longer pause for
            # names, addresses, and spelled contact details. A second VAD removed
            # meaningful parts of real email spelling, so preserve the whole clip
            # and spend more decoding effort on these accuracy-sensitive fields.
            return await asyncio.to_thread(run, vad_filter=False, beam_size=5, best_of=5)

        transcript = await asyncio.to_thread(run, vad_filter=True, beam_size=1, best_of=1)
        if transcript:
            return transcript

        # AudioSocket has already endpointed this clip with its energy VAD. Silero's
        # second VAD can reject valid one-word telephone answers such as "home",
        # "business", and "yes", so retry only empty results without that filter.
        duration_ms = int((audio.size / 16000) * 1000)
        logger.info("STT VAD returned no speech; retrying captured audio without VAD duration_ms=%d", duration_ms)
        return await asyncio.to_thread(run, vad_filter=False, beam_size=1, best_of=1)
