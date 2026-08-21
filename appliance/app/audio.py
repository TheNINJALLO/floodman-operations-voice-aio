from __future__ import annotations

import wave
from io import BytesIO

import numpy as np


def rms(pcm16le: bytes) -> int:
    if not pcm16le:
        return 0
    values = np.frombuffer(pcm16le, dtype="<i2").astype(np.float64)
    return int(np.sqrt(np.mean(values * values))) if values.size else 0


def pcm_to_wav(pcm16le: bytes, sample_rate: int = 8000) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16le)
    return buffer.getvalue()


def chunk_pcm(pcm16le: bytes, sample_rate: int = 8000, milliseconds: int = 20):
    size = max(2, int(sample_rate * milliseconds / 1000) * 2)
    size -= size % 2
    for index in range(0, len(pcm16le), size):
        chunk = pcm16le[index : index + size]
        if len(chunk) < size:
            chunk += b"\x00" * (size - len(chunk))
        yield chunk
