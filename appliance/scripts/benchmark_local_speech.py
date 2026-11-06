#!/usr/bin/env python3
"""Synthetic CPU speech round trips. No carrier, customer data or notifications."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
import time
import threading
import wave
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_models import configure_runtime_cache


async def validate_intake_recovery(settings, output, recognized_turns, contact_text):
    """Validate caller-visible behavior even when ASR splits a thought."""
    from app.business import BusinessDirectory
    from app.conversation import memory
    from app.db import Database
    from app.knowledge import KnowledgeBase
    from app.voice_core import VoiceCore
    from app.intake import normalize_email, normalize_phone
    class NoNotifications:
        async def send(self, *_args, **_kwargs): return 0
    class NoModel:
        async def answer(self, *_args): return ""
    root = Path(__file__).resolve().parents[1]
    core = VoiceCore(settings, Database(output / "synthetic-intake.sqlite"),
                     BusinessDirectory(root / "config/service_area.yaml"),
                     KnowledgeBase(root / "knowledge"), NoModel(), NoNotifications())
    call = core.create_session("synthetic-pause-" + uuid.uuid4().hex)
    call.state.stage = "anything_else"
    replies = [await core.process(call, text) for text in recognized_turns]
    pause_safe = bool(replies) and all(reply.wait_for_caller for reply in replies[:-1]) and not replies[-1].end_call and "TWO WEEKS" in call.state.description.upper()
    checks = {}
    exact = {}
    for field, text in contact_text.items():
        call = core.create_session("synthetic-contact-" + uuid.uuid4().hex)
        call.state.stage = field
        memory(call.state)["kind"] = "warranty"
        expected = "sam.jones@example.com" if field == "email" else "+12315550199"
        normalized = normalize_email(text) if field == "email" else normalize_phone(text)
        exact[field] = normalized == expected
        reply = await core.process(call, text)
        candidate = getattr(call.state, field)
        if call.state.stage == "confirm_" + field:
            # Merely recognizing a candidate must never confirm it.
            checks[field] = call.state.confirmations.get(field) != candidate and not reply.end_call
        else:
            # Unclear contact information must ask for recovery and stay unverified.
            checks[field] = not candidate and call.state.confirmations.get(field) is None and not reply.end_call
    return pause_safe, checks, exact


async def benchmark(output: Path, *, realtime=False, strict=False, contacts=False):
    from app.config import Settings
    from app.intake import detect_flood_emergency, normalize_email, normalize_phone
    from app.pocket_tts import PocketTTS
    from app.streaming_stt import StreamingLocalSTT
    settings = Settings.from_env()
    settings.tts_cache_enabled = False
    output.mkdir(parents=True, exist_ok=True)
    warnings = []
    class WarningRecorder(logging.Handler):
        def emit(self, record):
            if "Maximum generation length" in record.getMessage():
                warnings.append("generation_limit")
    handler = WarningRecorder()
    logging.getLogger("pocket_tts").addHandler(handler)
    started = time.perf_counter()
    tts, stt = PocketTTS(settings), StreamingLocalSTT(settings)
    class NoFallback:
        async def synthesize(self, *_args, **_kwargs):
            raise RuntimeError("The Pocket acceptance test unexpectedly used fallback speech")
    tts.fallback = NoFallback()
    await tts.start()
    await stt.start()
    # A repeatable speech fixture keeps unrelated source changes from selecting
    # a different synthetic accent/hesitation on each CI run. Production speech
    # keeps its normal sampling; the voice catalog checks varied presets too.
    import torch
    torch.manual_seed(4271)
    report = {"schema_version": 1, "python": platform.python_version(), "cpu": platform.processor(),
              "cpu_count": os.cpu_count(), "cold_start_seconds": round(time.perf_counter() - started, 3),
              "realtime_input": realtime, "speech_cache_enabled": False, "carrier_calls": 0, "staff_messages": 0,
              "uses_customer_data": False, "cloud_ai_requests": 0, "speech_seed": 4271, "cases": []}
    cases = [
        ("greeting", "issue", ("Hello, this is Avery, Floodman's digital assistant. How may I help you today?",), None),
        ("active-flood", "issue", ("Water is rising in my basement and I cannot stop the water.",), True),
        ("stopped-water", "issue", ("The water has stopped. My basement is just damp now.",), False),
        ("callback", "issue", ("Please ask Jim to call me after lunch.",), False),
        ("mid-sentence-pause", "anything_else", ("It's been", "happening every time it rains for two weeks."), False),
    ]
    recognized_turns = []
    if contacts:
        cases += [
            ("last-name", "last_name", ("My last name is Jones.",), None),
            ("email-address", "email", ("Sam dot Jones at example dot com.",), None),
            ("phone-number", "phone", ("My number is two three one, five five five, zero one nine nine.",), None),
        ]
    for name, stage, parts, expected_flood in cases:
        started = time.perf_counter()
        first_audio = None
        audio = bytearray()
        for index, text in enumerate(parts):
            if index:
                audio.extend(bytes(int(16000 * 0.9)))
            async for chunk in tts.stream_pcm(text):
                if first_audio is None:
                    first_audio = (time.perf_counter() - started) * 1000
                audio.extend(chunk)
        generation_ms = (time.perf_counter() - started) * 1000
        wav_path = output / (name + ".wav")
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(audio)

        async def frames():
            for offset in range(0, len(audio), 320):
                if realtime:
                    await asyncio.sleep(0.02)
                yield bytes(audio[offset:offset+320])
            for _ in range(90):
                if realtime:
                    await asyncio.sleep(0.02)
                yield bytes(320)

        started = time.perf_counter()
        turns = [turn async for turn in stt.stream_turns(frames(), get_stage=lambda: stage)]
        transcript = " ".join(turn.text for turn in turns)
        if name == "mid-sentence-pause":
            recognized_turns = [turn.text for turn in turns]
        actual_flood = detect_flood_emergency(transcript)
        item = {"case": name, "source_text": " ".join(parts), "recognized_text": transcript,
                "turn_count": len(turns), "audio_seconds": round(len(audio) / 16000, 3),
                "first_audio_ms": round(first_audio or 0, 2), "generation_ms": round(generation_ms, 2),
                "recognition_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "expected_flood": expected_flood, "recognized_flood": actual_flood,
                "audio_path": str(wav_path)}
        report["cases"].append(item)
        print(json.dumps(item), flush=True)
    report["generation_warnings"] = warnings
    cancellation = tts.stream_pcm("Please take your time. I can keep listening while you finish explaining what happened at the property.")
    await anext(cancellation)
    cancel_started = time.perf_counter()
    await cancellation.aclose()
    report["barge_in_cancel_ms"] = round((time.perf_counter() - cancel_started) * 1000, 2)
    report["cancelled_voice_worker_stopped"] = not any(thread.name == "pocket-local-voice" and thread.is_alive() for thread in threading.enumerate())
    report["routing_semantics_preserved"] = all(case["expected_flood"] is None or case["expected_flood"] == case["recognized_flood"] for case in report["cases"])
    contact_text = {}
    if contacts:
        recognized = {case["case"]: case["recognized_text"] for case in report["cases"]}
        contact_text = {"email": recognized["email-address"], "phone": recognized["phone-number"]}
    report["pause_preserved"], report["contact_checks"], report["contact_first_attempt_exact"] = await validate_intake_recovery(settings, output, recognized_turns, contact_text)
    if contacts:
        from app.conversation import last_name_answer
        report["contact_checks"]["last_name"] = last_name_answer(recognized["last-name"]).lower() == "jones"
    report["passed"] = all(case["recognized_text"] for case in report["cases"]) and report["routing_semantics_preserved"] and report["pause_preserved"] and all(report["contact_checks"].values()) and report["cancelled_voice_worker_stopped"] and report["barge_in_cancel_ms"] < 1500 and not warnings
    (output / "speech-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "routing_semantics_preserved", "pause_preserved", "contact_checks", "cancelled_voice_worker_stopped", "barge_in_cancel_ms", "generation_warnings")}), flush=True)
    logging.getLogger("pocket_tts").removeHandler(handler)
    print(json.dumps({"speech_trial": "passed" if report["passed"] else "needs_review", "carrier_calls": 0, "report": str(output / "speech-report.json")}), flush=True)
    if strict and not report["passed"]:
        raise SystemExit("Synthetic speech acceptance needs review")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--contacts", action="store_true")
    args = parser.parse_args()
    data = Path(os.environ["DATA_DIR"]).resolve()
    configure_runtime_cache(data)
    os.environ.update(LOCAL_VOICE_STACK="streaming_trial", PRIMARY_MODEL_PROFILE="haiku8b",
                      VOICE_ENGINE="tel_agent", LOCAL_AI_ONLY="true", LOCAL_AI_DEVICE="cpu",
                      SHERPA_WHISPER_FALLBACK="true" if args.contacts else "false", HF_HUB_OFFLINE="1")
    asyncio.run(benchmark(data / "benchmark", realtime=args.realtime, strict=args.strict, contacts=args.contacts))


if __name__ == "__main__":
    main()
