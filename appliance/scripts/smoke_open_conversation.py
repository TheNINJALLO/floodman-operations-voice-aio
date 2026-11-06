#!/usr/bin/env python3
"""Exercise open conversation with actual 8 kHz local speech and recognition."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_models import configure_runtime_cache


async def run(data: Path):
    from app.business import BusinessDirectory
    from app.config import Settings
    from app.db import Database
    from app.knowledge import KnowledgeBase
    from app.intake import classify_service
    from app.pocket_tts import PocketTTS
    from app.streaming_stt import StreamingLocalSTT
    from app.voice_core import VoiceCore

    settings = Settings.from_env()
    settings.tts_cache_enabled = False
    output = data / "open-conversation-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    class NoModel:
        async def answer(self, *args): return ""
    class NoNotifications:
        async def send(self, *args, **kwargs): return 0
    class NoFallback:
        async def synthesize(self, *args, **kwargs):
            raise RuntimeError("Synthetic acceptance must use Pocket TTS")
    core = VoiceCore(settings, Database(output / "synthetic.sqlite"),
                     BusinessDirectory(root / "config/service_area.yaml"),
                     KnowledgeBase(root / "knowledge"), NoModel(), NoNotifications())
    tts, stt = PocketTTS(settings), StreamingLocalSTT(settings)
    tts.fallback = NoFallback()
    await tts.start()
    await stt.start()
    import torch
    torch.manual_seed(4271)  # Repeatable fixture only; production is unseeded.
    checks, turns, fixture_audio = {}, [], {}

    async def audio(label, text):
        pcm = b"".join([part async for part in tts.stream_pcm(text)])
        assert pcm
        fixture_audio[label] = pcm
        with wave.open(str(output / (label + ".wav")), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(pcm)
        return pcm

    async def recognize(label, text, stage):
        pcm = await audio(label, text)
        async def frames():
            for offset in range(0, len(pcm), 320): yield pcm[offset:offset + 320]
            for _ in range(100): yield bytes(320)
        started = time.perf_counter()
        result = " ".join([turn.text async for turn in stt.stream_turns(frames(), get_stage=lambda: stage)])
        turns.append({"fixture": label, "stage": stage, "recognized_synthetic_text": result,
                      "offline_decode_ms": round((time.perf_counter() - started) * 1000, 2)})
        return result

    call = core.create_session("synthetic-open-" + uuid.uuid4().hex)
    problem = await recognize("problem", "There is mold in one of the bathrooms in my house.", "issue")
    reply = await core.process(call, problem)
    checks["problem_understood"] = call.state.service_key == "mold_remediation" and "bathroom" in problem.lower() and not reply.end_call
    recovered = await stt._recover_conversation("AM I HAVE A CULL THE HOUSE", fixture_audio["problem"], "issue")
    checks["real_decoder_recovers_unclear_stream"] = classify_service(recovered)["service_key"] == "mold_remediation" and "bathroom" in recovered.lower()
    call.state.stage = "name"
    continuation = await recognize("continuation", "It started last week and I'm worried about it spreading.", "name")
    reply = await core.process(call, continuation)
    checks["can_continue_story_before_name"] = not call.state.name and call.state.stage == "name" and not reply.end_call
    checks["new_detail_preserved"] = "week" in call.state.description.lower() and bool(call.state.timing_summary)

    call.state.stage = "anything_else"
    extra = await recognize("extra", "I just need a free estimate.", "anything_else")
    reply = await core.process(call, extra)
    checks["specific_acknowledgment"] = "inspection" in reply.text and "clarify" in reply.text and not reply.end_call
    await audio("specific-response", reply.text)
    thanks = await recognize("thanks", "Thank you so much.", "anything_else")
    reply = await core.process(call, thanks)
    checks["gratitude_closes_warmly"] = reply.end_call and "take care" in reply.text and "Goodbye" in reply.text
    checks["thanks_not_saved_as_extra_detail"] = "thank you" not in call.state.description.lower()
    await audio("farewell", reply.text)

    first = await audio("pause-start", "There is mold in the")
    second = await audio("pause-end", "bathroom, on the wall.")
    joined = first + bytes(700 * 16) + second
    async def paused_frames():
        for offset in range(0, len(joined), 320): yield joined[offset:offset + 320]
        for _ in range(100): yield bytes(320)
    paused = [turn.text async for turn in stt.stream_turns(paused_frames(), get_stage=lambda: "issue")]
    combined = " ".join(paused).lower()
    checks["thinking_pause_keeps_one_turn"] = len(paused) == 1 and classify_service(combined)["service_key"] == "mold_remediation" and "bathroom" in combined
    turns.append({"fixture": "thinking-pause", "recognized_synthetic_turns": paused})
    report = {"passed": all(checks.values()), "checks": checks, "turns": turns,
              "uses_customer_data": False, "cloud_ai_requests": 0, "carrier_calls": 0, "staff_messages": 0,
              "timing_note": "Offline synthetic decoding, not real telephone response latency."}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    if not report["passed"]:
        raise SystemExit("Open conversation speech acceptance needs review")


if __name__ == "__main__":
    data = Path(os.environ["DATA_DIR"]).resolve()
    configure_runtime_cache(data)
    os.environ.update(LOCAL_VOICE_STACK="streaming_trial", PRIMARY_MODEL_PROFILE="haiku8b",
                      VOICE_ENGINE="tel_agent", LOCAL_AI_ONLY="true", LOCAL_AI_DEVICE="cpu",
                      SHERPA_WHISPER_FALLBACK="true", HF_HUB_OFFLINE="1")
    asyncio.run(run(data))
