"""Local per-call diagnostics. No audio, credentials, or raw provider errors."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import platform
import threading
import time
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.db import Database, utcnow

logger = logging.getLogger(__name__)
current_trace: ContextVar[CallTrace | None] = ContextVar("call_diagnostic_trace", default=None)


def emit_current(event: str, **details: Any) -> None:
    trace = current_trace.get()
    if trace is not None:
        try:
            trace.emit(event, **details)
        except Exception:
            logger.warning("Unable to queue provider diagnostic event")

FEEDBACK_CATEGORIES = {
    "slow_response": "Long response delay",
    "cut_off": "Talked over the caller",
    "missing_audio": "Skipped speech or no voice",
    "transcription": "Heard the caller incorrectly",
    "wrong_response": "Incorrect or confusing reply",
    "notification": "Briefing or delivery problem",
    "other": "Another problem",
}

# Diagnostic events accept only operational values. Caller text and recipient
# details already have their own authenticated records, not a second log copy.
DETAIL_KEYS = {
    "voice_engine", "local_ai_device", "local_ai_only", "llm_model", "stt_model",
    "llm_provider", "stt_provider", "tts_provider", "voice", "voice_speed", "code_fingerprint",
    "stt_version", "tts_version", "onnx_version", "llm_model_file", "memory_limit_bytes",
    "python_version", "cpu_count", "cpu_model", "cpu_affinity_count", "cpu_quota_cores",
    "llm_threads", "stt_threads", "tts_threads", "endpoint_ms", "contact_endpoint_ms",
    "email_endpoint_ms", "no_input_seconds", "barge_in_ms", "barge_in_enabled",
    "vad_threshold", "barge_in_threshold", "input_preroll_ms", "post_tts_guard_ms",
    "duration_ms", "first_audio_ms", "audio_bytes", "audio_ms", "interrupted",
    "playback_fraction", "stage", "reason", "error_type", "outcome", "chars",
    "confidence", "language", "streaming_stt", "energy", "buffered_audio_bytes",
    "dropped_frames", "dropped_events", "voiced_ms", "silence_ms", "queue_depth",
    "slow_response_ms",
    "briefing_model", "post_call_briefings_enabled", "briefing_threads", "primary_model_profile", "briefing_backend",
}


def safe_details(values: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for key, value in values.items():
        if key not in DETAIL_KEYS:
            continue
        if isinstance(value, str):
            output[key] = value[:160]
        elif value is None or isinstance(value, (bool, int)):
            output[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            output[key] = round(value, 2)
    return output


def runtime_snapshot(settings: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    quota = None
    affinity = None
    cpu_model = platform.processor()
    memory_limit = None
    try:
        affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        with Path("/proc/cpuinfo").open(encoding="utf-8") as cpu_info:
            for line in cpu_info:
                if line.startswith("model name"):
                    cpu_model = line.partition(":")[2].strip()
                    break
    except OSError:
        pass
    try:
        budget, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if budget != "max":
            quota = int(budget) / int(period)
    except (OSError, ValueError, ZeroDivisionError):
        pass
    try:
        memory_limit = int(Path("/sys/fs/cgroup/memory.max").read_text().strip())
    except (OSError, ValueError):
        pass
    versions = {}
    for key, package in (("stt_version", "faster-whisper"), ("tts_version", "kokoro-onnx"), ("onnx_version", "onnxruntime")):
        try:
            versions[key] = version(package)
        except PackageNotFoundError:
            versions[key] = "unavailable"
    return safe_details({
        **versions,
        "code_fingerprint": digest.hexdigest()[:16], "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "cpu_model": cpu_model, "cpu_affinity_count": affinity,
        "cpu_quota_cores": quota, "memory_limit_bytes": memory_limit,
        "voice_engine": settings.voice_engine, "local_ai_device": settings.local_ai_device,
        "primary_model_profile": settings.primary_model_profile,
        "local_ai_only": settings.local_ai_only, "llm_model": settings.llama_model_alias,
        "stt_model": settings.faster_whisper_model, "voice": settings.kokoro_voice,
        "voice_speed": settings.kokoro_speed, "llm_model_file": settings.llm_model_path.name,
        "llm_threads": os.getenv("LLAMA_THREADS", "6"),
        "stt_threads": settings.faster_whisper_threads, "tts_threads": settings.kokoro_threads,
        "endpoint_ms": settings.endpoint_silence_ms,
        "contact_endpoint_ms": settings.contact_endpoint_silence_ms,
        "email_endpoint_ms": settings.email_endpoint_silence_ms,
        "no_input_seconds": settings.no_input_timeout_seconds,
        "barge_in_ms": settings.barge_in_min_speech_ms, "barge_in_enabled": settings.barge_in_enabled,
        "vad_threshold": settings.vad_energy_threshold, "barge_in_threshold": settings.barge_in_energy_threshold,
        "input_preroll_ms": settings.input_preroll_ms, "post_tts_guard_ms": settings.post_tts_guard_ms,
        "slow_response_ms": settings.call_slow_response_ms,
        "briefing_model": settings.briefing_model,
        "briefing_backend": settings.briefing_backend,
        "post_call_briefings_enabled": settings.post_call_briefings_enabled,
        "briefing_threads": (os.getenv("LLAMA_THREADS", "6") if settings.briefing_backend == "primary"
                             else settings.briefing_threads),
    })


class CallTrace:
    def __init__(self, store: CallDiagnostics, call_id: int):
        self.store = store
        self.call_id = call_id
        self.started = time.perf_counter()
        self.turn = 0
        self.dropped = 0

    def emit(self, event: str, *, level: str = "info", **details: Any) -> None:
        # This is on the audio path: never open SQLite, wait for a disk, or block.
        entry = {
            "call_id": self.call_id, "event": event[:64], "level": level,
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
            "turn": self.turn, "details": safe_details(details), "created_at": utcnow(),
        }
        if self.store.loop is not None and threading.get_ident() != self.store.loop_thread:
            self.store.loop.call_soon_threadsafe(self._enqueue, entry)
        else:
            self._enqueue(entry)

    def _enqueue(self, entry: dict[str, Any]) -> None:
        if self.dropped:
            entry["details"]["dropped_events"] = self.dropped
        try:
            self.store.queue.put_nowait(entry)
            self.dropped = 0
        except asyncio.QueueFull:
            self.dropped += 1
            self.store.dropped_events += 1


class CallDiagnostics:
    def __init__(self, database: Database, settings: Any):
        self.database = database
        self.settings = settings
        self.snapshot = runtime_snapshot(settings)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2048)
        self.task: asyncio.Task | None = None
        self.dropped_events = 0
        self.failed_events = 0
        self.inflight_events = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: int | None = None

    async def start(self) -> None:
        if self.task is None:
            self.loop = asyncio.get_running_loop()
            self.loop_thread = threading.get_ident()
            self.task = asyncio.create_task(self._write(), name="call-diagnostics")

    def begin(self, call_id: int, **providers: Any) -> CallTrace:
        trace = CallTrace(self, call_id)
        trace.emit("call_started", **{
            **self.snapshot, "voice": self.settings.kokoro_voice, "voice_speed": self.settings.kokoro_speed, **providers,
        })
        return trace

    async def _write(self) -> None:
        while True:
            batch = [await self.queue.get()]
            while len(batch) < 64:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                self.inflight_events = len(batch)
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(self.database.add_call_events, batch)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.05)
            except Exception as exc:
                self.failed_events += len(batch)
                # Diagnostics must fail open, and errors must not dump SQL,
                # provider headers, tokens, or customer information to logs.
                logger.warning("Call diagnostics write failed: %s", type(exc).__name__)
            finally:
                self.inflight_events = 0
                for _ in batch:
                    self.queue.task_done()

    async def stop(self) -> None:
        if self.task is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Call diagnostics shutdown flush timed out")
        finally:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def status(self) -> dict[str, int]:
        return {"queued_events": self.queue.qsize() + self.inflight_events, "discarded_events": self.dropped_events,
                "failed_events": self.failed_events}


def summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    events = diagnostics["events"]
    delays = [event["details"]["first_audio_ms"] for event in events
              if event["event"] == "first_audio" and event["turn"] > 0
              and isinstance(event["details"].get("first_audio_ms"), (int, float))]
    return {
        "responses": len(delays), "max_first_audio_ms": max(delays) if delays else None,
        "warnings": sum(event["level"] in {"warning", "error"} for event in events),
        "interruptions": sum(event["event"] == "barge_in" for event in events),
        "no_input": sum(event["event"] == "no_input" for event in events),
    }


def diagnostic_report(call: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1, "exported_at": utcnow(),
        "call": {key: call[key] for key in ("id", "call_uuid", "started_at", "ended_at", "status", "outcome", "current_stage")},
        "intake": {key: value for key, value in call["snapshot"].items() if key != "metadata"},
        "transcript": call["messages"], "delivery_attempts": call["notifications"],
        "diagnostics": diagnostics, "summary": summary(diagnostics),
        "coverage": {
            "audio_recording_included": False,
            "first_audio_timing": "From completed caller capture to first PCM frame written; includes local recognition, excludes endpoint silence and carrier playback buffering.",
            "note": "Live events may still be queued. Calls made before diagnostics was deployed have no saved timeline.",
        },
    }
