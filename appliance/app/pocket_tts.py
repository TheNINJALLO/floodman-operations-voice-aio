"""Opt-in CPU streaming voice with local-only weights and bounded cancellation."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import queue
import threading
import time
from collections.abc import AsyncIterator

from app.call_diagnostics import emit_current
from app.config import Settings
from app.local_trial_assets import manifest, prepare_pocket_config, verify_speech_assets
from app.streaming_audio import TelephoneResampler, tempo_stream
from app.tts import LocalTTS


VOICES = ("alba", "jean", "cosette", "fantine")


class PocketTTS:
    provider_name = "local-pocket-tts"
    streaming = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.states = {}
        self._start_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
        self._worker_lock = threading.Lock()
        self.fallback = LocalTTS(settings)
        self.settings_path = settings.data_dir / "pocket-voice-settings.json"
        self.asset_revision = hashlib.sha256(json.dumps([
            asset for asset in manifest()["assets"] if asset["path"].startswith("pocket/")
        ], sort_keys=True).encode()).hexdigest()
        try:
            saved = json.loads(self.settings_path.read_text())
            if saved.get("voice") in VOICES:
                settings.pocket_voice = saved["voice"]
                settings.pocket_speed = LocalTTS._validated_speed(saved.get("speed", 1.0))
        except (OSError, ValueError, TypeError):
            pass

    @property
    def current_voice(self):
        return "pocket_" + self.settings.pocket_voice

    @property
    def current_speed(self):
        return self.settings.pocket_speed

    @property
    def ready(self):
        return self.model is not None

    async def start(self):
        async with self._start_lock:
            if self.ready:
                return
            if self.settings.pocket_voice not in VOICES:
                raise ValueError("Select an installed Pocket voice")
            await asyncio.to_thread(verify_speech_assets, self.settings.local_trial_dir)
            config = prepare_pocket_config(self.settings.local_trial_dir)

            def load():
                import torch
                from pocket_tts import TTSModel
                torch.set_num_threads(self.settings.pocket_threads)
                with contextlib.suppress(RuntimeError):
                    torch.set_num_interop_threads(1)
                return TTSModel.load_model(config=str(config))

            self.model = await asyncio.to_thread(load)
            if self.model.sample_rate != 24000:
                self.model = None
                raise ValueError("Pinned Pocket voice must produce 24 kHz audio")
            await self._voice_state(self.settings.pocket_voice)

    async def _voice_state(self, voice):
        if voice not in VOICES:
            raise ValueError("Select an installed Pocket voice")
        if voice not in self.states:
            path = self.settings.local_trial_dir / "pocket" / (voice + ".safetensors")
            self.states[voice] = await asyncio.to_thread(self.model.get_state_for_audio_prompt, path)
        return self.states[voice]

    async def _generate(self, text, voice) -> AsyncIterator[bytes]:
        state = await self._voice_state(voice)
        output = queue.Queue(maxsize=4)
        stop = threading.Event()
        resampler = TelephoneResampler()

        def put(value):
            while not stop.is_set():
                try:
                    output.put(value, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def produce():
            # Pocket's model is not thread-safe. Retain this lock even if a
            # cancelled consumer returns before a native operation completes.
            with self._worker_lock:
                if stop.is_set():
                    return
                try:
                    generated = False
                    for samples in self.model.generate_audio_stream(state, text, copy_state=True, stop=stop):
                        if stop.is_set():
                            break
                        pcm = resampler.convert(samples.detach().cpu().numpy())
                        if pcm:
                            generated = True
                            put(pcm)
                    if not stop.is_set():
                        if not generated:
                            raise RuntimeError("Local voice produced no audio")
                        put(resampler.flush())
                except Exception as exc:
                    put(exc)
                finally:
                    put(None)

        worker = threading.Thread(target=produce, name="pocket-local-voice", daemon=True)
        worker.start()
        last_output = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.to_thread(output.get, True, 0.1)
                except queue.Empty:
                    if time.monotonic() - last_output > 2:
                        raise TimeoutError("Local voice generation stalled")
                    continue
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                last_output = time.monotonic()
                yield item
        finally:
            stop.set()
            await asyncio.to_thread(worker.join, 2)
            emit_current("tts_worker_stopped", outcome="stopped" if not worker.is_alive() else "finishing_native_frame")

    async def stream_pcm(self, text: str, *, speed=None, voice=None) -> AsyncIterator[bytes]:
        text = " ".join(str(text or "").split())
        if not text:
            return
        voice = voice or self.settings.pocket_voice
        speed = LocalTTS._validated_speed(self.current_speed if speed is None else speed)
        key = hashlib.sha256(f"pocket-4.0.1|{self.asset_revision}|{voice}|{speed}|{text}".encode()).hexdigest()
        path = self.settings.cache_dir / "pocket" / (key + ".pcm")
        if self.settings.tts_cache_enabled and path.is_file():
            emit_current("tts_cache_hit")
            yield path.read_bytes()
            return
        await self.start()
        emitted = False
        collected = bytearray()
        async with self._generation_lock:
            stream = tempo_stream(self._generate(text, voice), speed)
            try:
                async for pcm in stream:
                    if pcm:
                        emitted = True
                        collected.extend(pcm)
                        yield pcm
            except Exception as exc:
                if emitted:
                    raise
                emit_current("tts_fallback", level="warning", reason="local_kokoro", error_type=type(exc).__name__)
                yield await self.fallback.synthesize(text, speed=speed)
                return
            finally:
                await stream.aclose()
        # A cancelled or failed stream can never become a cached half-question.
        if collected and self.settings.tts_cache_enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            temporary.write_bytes(collected)
            temporary.replace(path)

    def stream(self, text):
        return self.stream_pcm(text)

    async def synthesize(self, text, *, speed=None):
        return b"".join([chunk async for chunk in self.stream_pcm(text, speed=speed)])

    async def warm(self, phrases):
        await self.start()
        for phrase in phrases:
            await self.synthesize(phrase)

    async def available_voices(self):
        return ["pocket_" + voice for voice in VOICES]

    async def preview(self, text, voice, speed):
        if voice not in await self.available_voices() or not text.strip() or len(text) > 240:
            raise ValueError("Select an installed voice and enter up to 240 characters")
        return b"".join([part async for part in self.stream_pcm(text, voice=voice.removeprefix("pocket_"), speed=speed)])

    async def configure(self, voice, speed):
        if voice not in await self.available_voices():
            raise ValueError("Select an installed Pocket voice")
        speed = LocalTTS._validated_speed(speed)
        async with self._generation_lock:
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"voice": voice.removeprefix("pocket_"), "speed": speed}), encoding="utf-8")
            temporary.replace(self.settings_path)
            self.settings.pocket_voice = voice.removeprefix("pocket_")
            self.settings.pocket_speed = speed
        return voice, speed
