"""Continuous 24 kHz-to-telephone PCM conversion and optional tempo control."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import numpy as np
from scipy.signal import firwin, lfilter


class TelephoneResampler:
    """Keep filter history and decimation phase across Pocket audio chunks."""

    def __init__(self):
        self.taps = firwin(63, 1 / 3).astype(np.float32)
        self.history = np.zeros(62, dtype=np.float32)
        self.samples_seen = 0

    def convert(self, samples) -> bytes:
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not len(values):
            return b""
        filtered, self.history = lfilter(self.taps, [1.0], values, zi=self.history)
        offset = (-self.samples_seen) % 3
        self.samples_seen += len(values)
        audio = np.clip(filtered[offset::3], -1, 1)
        return (audio * 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:
        return self.convert(np.zeros(63, dtype=np.float32))


async def tempo_stream(source: AsyncIterator[bytes], speed: float) -> AsyncIterator[bytes]:
    if speed == 1.0:
        try:
            async for chunk in source:
                yield chunk
        finally:
            close = getattr(source, "aclose", None)
            if close:
                await close()
        return
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
        "-af", f"atempo={speed:.2f}", "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def feed():
        try:
            async for chunk in source:
                process.stdin.write(chunk)
                await process.stdin.drain()
        finally:
            process.stdin.close()

    producer = asyncio.create_task(feed())
    try:
        while chunk := await process.stdout.read(1280):
            yield chunk
        await producer
        if await process.wait() != 0:
            raise RuntimeError("Local speech tempo conversion failed")
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        close = getattr(source, "aclose", None)
        if close:
            with contextlib.suppress(Exception):
                await close()
        if process.returncode is None:
            process.kill()
            await process.wait()
