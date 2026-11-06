#!/usr/bin/env python3
"""Render real CPU email confirmations and recognize a natural closing answer."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
import sys
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_models import configure_runtime_cache


async def run(data: Path):
    from app.business import BusinessDirectory
    from app.config import Settings
    from app.db import Database
    from app.knowledge import KnowledgeBase
    from app.local_speaker import speak
    from app.models import VoiceReply
    from app.pocket_tts import PocketTTS
    from app.streaming_stt import StreamingLocalSTT
    from app.voice_core import VoiceCore

    settings = Settings.from_env()
    settings.tts_cache_enabled = False
    settings.email_readback_speed = 1.0
    output = data / "email-closing-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]

    class NoModel:
        async def answer(self, *_args): return ""

    class NoNotifications:
        async def send(self, *_args, **_kwargs): return 0

    class NoFallback:
        async def synthesize(self, *_args, **_kwargs):
            raise RuntimeError("Pocket acceptance unexpectedly used a fallback voice")

    core = VoiceCore(settings, Database(output / "synthetic.sqlite"),
                     BusinessDirectory(root / "config/service_area.yaml"),
                     KnowledgeBase(root / "knowledge"), NoModel(), NoNotifications())
    tts, stt = PocketTTS(settings), StreamingLocalSTT(settings)
    tts.fallback = NoFallback()
    await tts.start()
    await stt.start()
    call = core.create_session("synthetic-email-" + uuid.uuid4().hex)
    call.state.stage = "email"
    natural = await core.process(call, "sam.jones@example.com")
    old_text = "I heard s, a, m, dot, j, o, n, e, s, at, e, x, a, m, p, l, e, dot, c, o, m"
    legacy = VoiceReply(text=old_text + ". Is that correct?",
                        speech_parts=(old_text, "Is that correct?"),
                        speech_part_speeds=(0.78, None), pause_between_parts_ms=300)

    async def render(name, reply):
        class Sink:
            pcm = bytearray()
            async def write(self, pcm): self.pcm.extend(pcm)
            async def flush(self): pass

        async def text(): yield reply.text
        sink = Sink()
        result = await speak(text(), tts=tts, sink=sink, should_stop=lambda: False,
                             speech_parts=reply.speech_parts, part_speeds=reply.speech_part_speeds,
                             pause_ms=reply.pause_between_parts_ms)
        pcm = bytes(sink.pcm)
        with wave.open(str(output / (name + ".wav")), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(pcm)
        return pcm, {"audio_seconds": round(len(pcm) / 16000, 3), "first_audio_ms": round(result.first_audio_ms or 0, 2)}

    async def recognize(pcm, stage="anything_else"):
        async def frames():
            for offset in range(0, len(pcm), 320): yield pcm[offset:offset + 320]
            for _ in range(90): yield bytes(320)
        return " ".join([turn.text async for turn in stt.stream_turns(frames(), get_stage=lambda: stage)])

    pcm, natural_metrics = await render("natural-email", natural)
    recognized_email = await recognize(pcm, stage="email")
    _, legacy_metrics = await render("legacy-spelled-email", legacy)
    pcm, closing_metrics = await render("natural-closing", VoiceReply(text="That should be it."))
    recognized_closing = await recognize(pcm)
    call.state.stage = "anything_else"
    reply = await core.process(call, recognized_closing)
    checks = {
        "natural_readback_shorter": natural_metrics["audio_seconds"] < legacy_metrics["audio_seconds"] * 0.9,
        "email_words_intelligible": all(word in re.sub(r"[^a-z]", "", recognized_email.lower()) for word in ("sam", "jones", "example", "com")),
        "email_not_auto_confirmed": "email" not in call.state.confirmations,
        "spoken_closing_ends_without_repeat": reply.end_call and "anything else" not in reply.text.lower(),
    }
    report = {"passed": all(checks.values()), "checks": checks, "natural": natural_metrics,
              "legacy": legacy_metrics, "closing": closing_metrics,
              "recognized_synthetic_email": recognized_email, "recognized_synthetic_closing": recognized_closing,
              "uses_customer_data": False, "cloud_ai_requests": 0, "carrier_calls": 0, "staff_messages": 0}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    if not report["passed"]:
        raise SystemExit("Email and closing speech acceptance needs review")


if __name__ == "__main__":
    data = Path(os.environ["DATA_DIR"]).resolve()
    configure_runtime_cache(data)
    os.environ.update(LOCAL_VOICE_STACK="streaming_trial", PRIMARY_MODEL_PROFILE="haiku8b",
                      VOICE_ENGINE="tel_agent", LOCAL_AI_ONLY="true", LOCAL_AI_DEVICE="cpu",
                      SHERPA_WHISPER_FALLBACK="true", HF_HUB_OFFLINE="1")
    asyncio.run(run(data))
