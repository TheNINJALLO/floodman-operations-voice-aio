from types import SimpleNamespace

import numpy as np
import pytest

from app.stt import LocalSTT


class StubWhisperModel:
    def __init__(self, *, filtered_text: str, unfiltered_text: str):
        self.filtered_text = filtered_text
        self.unfiltered_text = unfiltered_text
        self.vad_calls: list[bool] = []
        self.options: list[dict] = []

    def transcribe(self, _audio, **options):
        use_vad = options["vad_filter"]
        self.vad_calls.append(use_vad)
        self.options.append(options)
        text = self.filtered_text if use_vad else self.unfiltered_text
        segments = [SimpleNamespace(text=text)] if text else []
        return segments, SimpleNamespace()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["home", "business", "yes"])
async def test_short_answer_retries_without_redundant_whisper_vad(answer):
    stt = LocalSTT(SimpleNamespace())
    model = StubWhisperModel(filtered_text="", unfiltered_text=answer)
    stt._model = model
    pcm = (np.ones(8000, dtype="<i2") * 1000).tobytes()

    assert await stt.transcribe(pcm, 8000) == answer
    assert model.vad_calls == [True, False]


@pytest.mark.asyncio
async def test_successful_filtered_transcript_does_not_retry():
    stt = LocalSTT(SimpleNamespace())
    model = StubWhisperModel(filtered_text="water in my basement", unfiltered_text="unused")
    stt._model = model
    pcm = (np.ones(8000, dtype="<i2") * 1000).tobytes()

    assert await stt.transcribe(pcm, 8000) == "water in my basement"
    assert model.vad_calls == [True]


@pytest.mark.asyncio
async def test_contact_details_use_full_audio_and_more_accurate_decoding():
    stt = LocalSTT(SimpleNamespace())
    model = StubWhisperModel(filtered_text="dot com", unfiltered_text="josh at example dot com")
    stt._model = model
    pcm = (np.ones(16000, dtype="<i2") * 1000).tobytes()

    assert await stt.transcribe(pcm, 8000, contact=True) == "josh at example dot com"
    assert model.vad_calls == [False]
    assert model.options[0]["beam_size"] == 5
    assert model.options[0]["best_of"] == 5
