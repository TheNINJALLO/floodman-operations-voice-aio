"""Play complete local phrases continuously and cancel synthesis on barge-in."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class LocalSpokenTurn:
    text: str = ""
    first_audio_ms: float | None = None
    interrupted: bool = False


async def speak(text_stream, *, tts, sink, should_stop, since=None,
                speech_parts=(), part_speeds=(), pause_ms=0):
    started = since if since is not None else time.perf_counter()
    text = "".join([part async for part in text_stream]).strip()
    parts = speech_parts if len(speech_parts) > 1 else (text,)
    result = LocalSpokenTurn()
    spoken = []
    for index, part in enumerate(parts):
        if should_stop():
            result.interrupted = True
            break
        speed = part_speeds[index] if index < len(part_speeds) else None
        stream = tts.stream_pcm(part, speed=speed)
        sent = False
        try:
            async for pcm in stream:
                if should_stop():
                    result.interrupted = True
                    break
                if pcm:
                    if result.first_audio_ms is None:
                        result.first_audio_ms = (time.perf_counter() - started) * 1000
                    await sink.write(pcm)
                    sent = True
                if should_stop():
                    result.interrupted = True
                    break
        finally:
            await stream.aclose()
        if sent:
            spoken.append(part)
        if result.interrupted:
            await sink.flush()
            break
        if pause_ms and index < len(parts) - 1:
            # Paced silence remains interruptible through the same PCM sink.
            for _ in range(0, pause_ms, 20):
                if should_stop():
                    result.interrupted = True
                    break
                await sink.write(b"\x00\x00" * int(8000 * min(20, pause_ms) / 1000))
        if result.interrupted:
            break
    result.text = " ".join(spoken)
    return result
