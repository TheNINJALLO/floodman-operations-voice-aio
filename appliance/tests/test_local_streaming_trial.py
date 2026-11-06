from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.briefings import PostCallBriefings
from app.config import Settings
from app.db import Database
from app.local_trial_assets import asset_path, manifest, prepare_pocket_config, verify_asset
from app.local_speaker import speak
from app.model_profile import select_primary_model
from app.pocket_tts import PocketTTS
from app.silero_gate import SileroGate
from app.streaming_audio import TelephoneResampler
from app.streaming_stt import StreamingLocalSTT, endpoint_ms
from app.tel_agent_runtime import TelAgentLLM
from app.trial_wording import valid_question_rewrite


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHERPA_WHISPER_FALLBACK", "false")
    return Settings.from_env()


def test_trial_is_explicit_and_preserves_deckard_default(settings, monkeypatch):
    assert settings.local_voice_stack == "standard" and settings.primary_model_profile == "deckard"
    monkeypatch.setenv("LOCAL_VOICE_STACK", "streaming_trial")
    monkeypatch.setenv("PRIMARY_MODEL_PROFILE", "haiku8b")
    monkeypatch.setenv("LLAMA_MODEL_FILENAME", "stale-qwen.gguf")
    selected = Settings.from_env()
    model = select_primary_model(selected.model_dir)
    assert selected.local_ai_only and selected.local_ai_device == "cpu"
    assert model.filename.endswith("i1-Q4_K_M.gguf") and model.size_bytes == 4920736032
    assert model.sha256 == "29f01eee4ba45829d895620391e0c8b27df68bffc1da276cf5b2ad88744f7c5e"
    monkeypatch.setenv("GROQ_API_KEY", "must-not-be-used")
    assert TelAgentLLM(selected).provider_name == "tel-agent-local-llama"


@pytest.mark.parametrize("key,value", [("LOCAL_AI_ONLY", "false"), ("VOICE_ENGINE", "local"), ("LOCAL_AI_DEVICE", "cuda")])
def test_trial_rejects_incompatible_or_external_profiles(settings, monkeypatch, key, value):
    monkeypatch.setenv("LOCAL_VOICE_STACK", "streaming_trial")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="trial"):
        Settings.from_env()


def test_assets_are_pinned_and_matching_voice_states_are_selected(settings):
    assets = manifest()["assets"]
    assert all(len(asset["sha256"]) == 64 and asset["size_bytes"] > 0 for asset in assets)
    voices = [asset for asset in assets if asset["path"].startswith("pocket/") and asset["path"] != "pocket/model.safetensors" and asset["path"].endswith(".safetensors")]
    assert len(voices) == 19 and all("/languages/english/embeddings/" in asset["url"] for asset in voices)
    with pytest.raises(ValueError):
        asset_path(settings.local_trial_dir, "../../../../outside")
    config = prepare_pocket_config(settings.local_trial_dir).read_text()
    assert "hf://" not in config and "https://" not in config
    assert str(settings.local_trial_dir.resolve()).replace("\\", "\\\\") in config or "model.safetensors" in config
    path = settings.data_dir / "asset"
    path.write_bytes(b"good")
    pin = {"size_bytes": 4, "sha256": hashlib.sha256(b"good").hexdigest()}
    assert verify_asset(path, pin)
    path.write_bytes(b"evil")
    assert not verify_asset(path, pin)


def test_silero_windows_and_state_are_per_caller(settings):
    class Session:
        def __init__(self): self.inputs = []
        def run(self, _outputs, inputs):
            self.inputs.append(inputs)
            return np.array([[0.75]], np.float32), inputs["state"] + 1
    model = Session()
    first = SileroGate(Path("unused"), session=model)
    second = SileroGate(Path("unused"), session=model)
    assert first.probability(bytes(320)) is None
    assert first.probability(bytes(320)) == 0.75
    assert model.inputs[-1]["input"].shape == (1, 288)
    assert second.last_probability is None and not second.state.any()
    assert first.state.min() == 1
    first.reset()
    assert not first.state.any() and not len(first.pending)


@pytest.mark.asyncio
async def test_speech_gate_setup_failure_preserves_the_local_audio_connection(settings, monkeypatch):
    from app.audiosocket import AudioSocketConnection
    from test_audiosocket_protocol import BufferWriter
    def broken(*_args, **_kwargs): raise RuntimeError("unavailable")
    monkeypatch.setattr("app.silero_gate.SileroGate", broken)
    settings.local_voice_stack = "streaming_trial"
    connection = AudioSocketConnection(asyncio.StreamReader(), BufferWriter(), settings)
    assert not connection.closed and connection._barge_gate is None
    assert connection._voiced((1000).to_bytes(2, "little", signed=True) * 160, barge=True)
    await connection.close()


def test_streaming_resampler_matches_unsplit_waveform():
    source = np.random.default_rng(2).normal(0, 0.1, 12345).astype(np.float32)
    whole = TelephoneResampler()
    expected = whole.convert(source) + whole.flush()
    chunked = TelephoneResampler()
    actual = b"".join(chunked.convert(source[i:i+197]) for i in range(0, len(source), 197)) + chunked.flush()
    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="The CPU image supplies ffmpeg")
async def test_readback_tempo_preserves_pitch_while_slowing_speech():
    from app.streaming_audio import tempo_stream
    tone = (np.sin(2 * np.pi * 440 * np.arange(16000) / 8000) * 12000).astype("<i2").tobytes()
    async def source():
        for index in range(0, len(tone), 320): yield tone[index:index+320]
    output = b"".join([part async for part in tempo_stream(source(), 0.8)])
    samples = np.frombuffer(output, dtype="<i2")
    assert abs(len(samples) / 8000 - 2.5) < 0.2
    peak = np.argmax(abs(np.fft.rfft(samples))) * 8000 / len(samples)
    assert abs(peak - 440) < 5


def test_endpointing_preserves_fast_confirmations_and_spelled_contacts(settings):
    assert endpoint_ms(settings, "confirm_phone") == settings.endpoint_silence_ms
    assert endpoint_ms(settings, "email") >= 1600
    assert endpoint_ms(settings, "anything_else") >= 600
    assert endpoint_ms(settings, "anything_else", "it's been") >= 1400
    assert endpoint_ms(settings, "anything_else", "it's been at first") >= 1400
    assert endpoint_ms(settings, "anything_else", "it's been two weeks") == 600


@pytest.mark.parametrize("candidate,expected", [
    ("Is the water still coming in, or has it stopped?", True),
    ("Which room is affected?", False),
    ("Is the water rising? Is the electrical panel wet?", False),
    ("I've dispatched someone. Is the water rising?", False),
    ("Is the water rising above 3 feet?", False),
])
def test_trial_wording_preserves_requested_question_and_cannot_claim_actions(candidate, expected):
    assert valid_question_rewrite(candidate, "Is water still coming in or rising, or has it stopped?", "water_condition") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate,accepted", [
    ("Is the water still coming in, or has it stopped?", True),
    ("I'll connect you now. Is water rising?", False),
    ("Which room has an electrical hazard?", False),
])
async def test_trial_can_rephrase_one_question_but_rejects_changed_actions(candidate, accepted):
    from test_tel_agent_phone import agent, session
    from app.models import VoiceReply
    phone = agent((candidate,))
    phone.core.settings = SimpleNamespace(local_voice_stack="streaming_trial")
    original = "Is water still coming in or rising, or has it stopped?"
    reply = VoiceReply(text=original, speech_parts=(original,))
    result = "".join([part async for part in phone._reply_stream(session("water_condition"), "Water in my basement", reply, "issue")])
    assert result == (candidate if accepted else original)
    assert reply.speech_parts == (original,) and not reply.transfer_number


@pytest.mark.asyncio
async def test_trial_never_rewrites_an_emergency_handoff():
    from test_tel_agent_phone import agent, session
    from app.models import VoiceReply
    phone = agent(("A model must not decide a different action.",))
    phone.core.settings = SimpleNamespace(local_voice_stack="streaming_trial")
    reply = VoiceReply(text="I'm connecting you with Floodman's emergency contact now.", transfer_number="+12315550101")
    assert "".join([part async for part in phone._reply_stream(session("issue", "emergency"), "Water is rising", reply, "issue")]) == reply.text


@pytest.mark.asyncio
async def test_deckard_briefings_can_use_a_separate_private_model(settings):
    trial = replace(settings, briefing_backend="local_api", briefing_llama_url="http://127.0.0.1:8082/v1",
                    briefing_model="floodman-deckard:40b-q4_k_m", llama_model_alias="floodman-haiku-distill:8b-q4_k_m")
    service = PostCallBriefings(trial, Database(trial.database_path), lambda: 0)
    assert service.client.model == "floodman-deckard:40b-q4_k_m"
    assert service.client.llm.settings.llama_base_url == "http://127.0.0.1:8082/v1"
    assert trial.llama_model_alias == "floodman-haiku-distill:8b-q4_k_m"
    rejected = replace(trial, briefing_llama_url="https://example.com/v1")
    assert PostCallBriefings(rejected, service.database, lambda: 0).configuration_error


class Samples:
    def detach(self): return self
    def cpu(self): return self
    def numpy(self): return np.ones(1920, dtype=np.float32) * 0.1


@pytest.mark.asyncio
async def test_pocket_barge_in_stops_generation_and_never_caches_partial_audio(settings):
    stopped = threading.Event()
    class Model:
        def generate_audio_stream(self, _state, _text, *, copy_state, stop):
            assert copy_state
            try:
                for _ in range(1000):
                    if stop.is_set(): break
                    time.sleep(0.002)
                    yield Samples()
            finally:
                stopped.set()
    tts = PocketTTS(settings)
    tts.model = Model()
    tts.states["alba"] = {"preset": "unchanged"}
    stream = tts.stream_pcm("A caller interrupts this sentence.")
    assert await anext(stream)
    await stream.aclose()
    assert stopped.is_set()
    assert not list((settings.cache_dir / "pocket").glob("*.pcm"))
    assert tts.states["alba"] == {"preset": "unchanged"}


@pytest.mark.asyncio
async def test_empty_pocket_generation_uses_local_fallback_instead_of_caching_silence(settings):
    class Model:
        def generate_audio_stream(self, *_args, **_kwargs):
            return iter(())
    class Fallback:
        async def synthesize(self, text, *, speed): return b"fallback-pcm"
    tts = PocketTTS(settings)
    tts.model = Model()
    tts.states["alba"] = {}
    tts.fallback = Fallback()
    assert await tts.synthesize("A complete greeting.") == b"fallback-pcm"
    assert not list((settings.cache_dir / "pocket").glob("*.pcm"))


@pytest.mark.asyncio
async def test_local_speaker_preserves_prepared_readback_parts_and_stops_on_interruption():
    produced, closed, written = [], [], []
    interrupted = False
    class TTS:
        async def stream_pcm(self, text, *, speed=None):
            produced.append((text, speed))
            try:
                yield bytes(320)
                yield bytes(320)
            finally:
                closed.append(text)
    class Sink:
        async def write(self, pcm):
            nonlocal interrupted
            written.append(pcm)
            if len(written) == 3: interrupted = True
        async def flush(self): pass
    async def text(): yield "one two three. Is that correct?"
    result = await speak(text(), tts=TTS(), sink=Sink(), should_stop=lambda: interrupted,
                         speech_parts=("one two three", "Is that correct?"), part_speeds=(0.78, None))
    assert produced == [("one two three", 0.78), ("Is that correct?", None)]
    assert result.interrupted and len(written) == 3 and closed == ["one two three", "Is that correct?"]


class FakeRecognizer:
    def __init__(self, text): self.text, self.created = text, []
    def create_stream(self):
        stream = SimpleNamespace(accept_waveform=lambda *_: None, input_finished=lambda: None)
        self.created.append(stream)
        return stream
    def is_ready(self, stream): return False
    def get_result(self, stream): return self.text


@pytest.mark.asyncio
async def test_streaming_email_is_not_split_at_an_internal_pause(settings, monkeypatch):
    class Gate:
        def __init__(self, *_): pass
        def probability(self, frame): return float(any(frame))
    monkeypatch.setattr("app.streaming_stt.SileroGate", Gate)
    stt = StreamingLocalSTT(settings)
    stt.recognizer = FakeRecognizer("sam at example dot com")
    voice = (1000).to_bytes(2, "little", signed=True) * 160
    async def frames():
        for frame in [voice]*10 + [bytes(320)]*45 + [voice]*10 + [bytes(320)]*90:
            yield frame
    turns = [turn async for turn in stt.stream_turns(frames(), get_stage=lambda: "email")]
    assert len(turns) == 1 and turns[0].text == "sam at example dot com"


@pytest.mark.asyncio
async def test_non_speech_cannot_become_a_recognized_confirmation(settings, monkeypatch):
    class Gate:
        def __init__(self, *_): pass
        def probability(self, frame): return 0.0
    monkeypatch.setattr("app.streaming_stt.SileroGate", Gate)
    stt = StreamingLocalSTT(settings)
    stt.recognizer = FakeRecognizer("yes")
    async def noise():
        for _ in range(80): yield bytes([1, 0]) * 160
    assert [turn async for turn in stt.stream_turns(noise(), get_stage=lambda: "confirm_phone")] == []
