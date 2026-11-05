"""Stage-aware local streaming ASR; no provider socket or business authority."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

import numpy as np

from app.call_diagnostics import emit_current
from app.config import Settings
from app.conversation import unfinished_turn
from app.local_trial_assets import verify_speech_assets
from app.audio import rms
from app.speech_activity import SpeechActivity
from app.silero_gate import SileroGate, load_silero_session
from app.stt import LocalSTT
from app.tel_agent_runtime import TelAgentTranscriptTurn


def endpoint_ms(settings: Settings, stage: str, text: str = "") -> int:
    if stage == "email":
        result = settings.email_endpoint_silence_ms
    elif stage.startswith("confirm_"):
        result = settings.endpoint_silence_ms
    elif stage in {"name", "phone", "address"}:
        result = settings.contact_endpoint_silence_ms
    else:
        result = max(settings.endpoint_silence_ms, settings.conversation_endpoint_silence_ms)
    if text and unfinished_turn(text):
        result = max(result, 1400)
    return result


class StreamingLocalSTT:
    streaming = True
    stage_aware = True
    activity_aware = True
    provider_name = "local-sherpa-zipformer-int8"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.recognizer = None
        self.fallback = LocalSTT(settings)
        self._start_lock = asyncio.Lock()
        self._decode_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.recognizer is not None and (not self.settings.sherpa_whisper_fallback or self.fallback._model is not None)

    async def start(self) -> None:
        async with self._start_lock:
            if self.ready:
                return
            await asyncio.to_thread(verify_speech_assets, self.settings.local_trial_dir)
            await asyncio.to_thread(load_silero_session, str((self.settings.local_trial_dir / "vad/silero.onnx").resolve()))

            def load():
                import sherpa_onnx
                root = self.settings.local_trial_dir / "asr"
                return sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=str(root / "tokens.txt"), encoder=str(root / "encoder.onnx"),
                    decoder=str(root / "decoder.onnx"), joiner=str(root / "joiner.onnx"),
                    num_threads=self.settings.sherpa_threads, provider="cpu",
                    sample_rate=16000, enable_endpoint_detection=False,
                )

            self.recognizer = await asyncio.to_thread(load)
            if self.settings.sherpa_whisper_fallback:
                await self.fallback.start()

    async def transcribe_local(self, pcm: bytes, sample_rate=8000, *, contact=False) -> str:
        # Used only if the continuous recognizer fails. It retains the existing
        # local contact spelling path and cannot enable an external recognizer.
        return await self.fallback.transcribe(pcm, sample_rate, contact=contact)

    def _decode(self, stream, frame: bytes = b"", *, final=False) -> str:
        if frame:
            samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
            stream.accept_waveform(8000, samples)
        if final:
            stream.accept_waveform(8000, np.zeros(2400, dtype=np.float32))
            stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return self.recognizer.get_result(stream).strip()

    async def stream_turns(self, audio: AsyncIterator[bytes], *, get_stage: Callable[[], str] = lambda: "issue", activity: SpeechActivity | None = None) -> AsyncIterator[TelAgentTranscriptTurn]:
        await self.start()
        activity = activity if activity is not None else SpeechActivity()
        stream = self.recognizer.create_stream()
        gate = SileroGate(self.settings.local_trial_dir / "vad/silero.onnx")
        buffer = bytearray()
        voiced_ms = silence_ms = elapsed_ms = idle_ms = 0.0
        active = False
        stage = "issue"
        last_speech_at = None
        previous_text = ""

        async def finish() -> TelAgentTranscriptTurn | None:
            async with self._decode_lock:
                text = await asyncio.to_thread(self._decode, stream, final=True)
            if voiced_ms < min(80, self.settings.minimum_speech_ms):
                return None
            contact = stage.removeprefix("confirm_") in {"name", "phone", "address", "email"}
            # Contact values retain the established higher-accuracy decoder.
            # Streaming partials are for turn timing, never verified facts.
            if self.settings.sherpa_whisper_fallback and (contact or not text):
                emit_current("stt_fallback", reason="local_whisper_contact" if contact else "empty_stream_result")
                text = await self.fallback.transcribe(bytes(buffer), 8000, contact=contact)
            emit_current("stt_completed", chars=len(text), stage=stage, reason="stream_endpoint")
            return TelAgentTranscriptTurn(text=text, audio_ended_at=last_speech_at, heard_audio=True)

        async for frame in audio:
            if not frame:
                continue
            if activity.reset_requested:
                stream = self.recognizer.create_stream()
                buffer.clear()
                voiced_ms = silence_ms = elapsed_ms = idle_ms = 0
                active = False
                previous_text = ""
                gate.reset()
                activity.reset_requested = False
            if len(frame) % 2:
                raise ValueError("Local recognizer requires complete PCM16 samples")
            duration = len(frame) / 16
            probability = gate.probability(frame)
            voiced = probability is not None and probability >= self.settings.silero_threshold and rms(frame) >= max(80, min(1500, activity.background_energy * 1.5))
            async with self._decode_lock:
                text = await asyncio.to_thread(self._decode, stream, frame)
            if text and text != previous_text:
                emit_current("stt_partial", chars=len(text), stage=get_stage())
                previous_text = text
            if voiced:
                activity.voiced()
                if not active:
                    stage = get_stage()
                    active = True
                    emit_current("speech_started", stage=stage)
                voiced_ms += duration
                silence_ms = 0
                last_speech_at = time.perf_counter()
            elif active:
                silence_ms += duration
            if active:
                elapsed_ms += duration
                buffer.extend(frame)
                ended = silence_ms >= endpoint_ms(self.settings, stage, text)
                limited = elapsed_ms >= self.settings.maximum_utterance_seconds * 1000
                if ended or limited:
                    activity.decoding = True
                    try:
                        turn = await finish()
                    finally:
                        activity.decoding = False
                    if turn is not None:
                        yield turn
                    stream = self.recognizer.create_stream()
                    buffer.clear()
                    voiced_ms = silence_ms = elapsed_ms = idle_ms = 0
                    active = False
                    previous_text = ""
            else:
                # Preserve a short onset for the optional contact decoder.
                buffer.extend(frame)
                del buffer[:max(0, len(buffer) - int(16 * self.settings.input_preroll_ms))]
                idle_ms += duration
                if idle_ms >= self.settings.no_input_timeout_seconds * 1000:
                    stream = self.recognizer.create_stream()
                    idle_ms = 0
                    previous_text = ""
        if active:
            turn = await finish()
            if turn is not None:
                yield turn
