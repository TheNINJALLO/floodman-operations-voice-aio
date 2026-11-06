#!/usr/bin/env python3
"""Exercise real Silero interruption detection using synthetic speech and noise."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audiosocket import AudioSocketConnection
from app.config import Settings


class Writer:
    def write(self, _): pass
    async def drain(self): pass


def load(path):
    with wave.open(str(path), 'rb') as source:
        assert source.getframerate() == 8000 and source.getsampwidth() == 2
        return np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32)


def main():
    settings = Settings.from_env()
    settings.local_voice_stack = 'streaming_trial'
    settings.barge_in_min_speech_ms = 320
    settings.barge_in_energy_threshold = 500
    settings.barge_in_vad_threshold = .75
    settings.barge_in_max_gap_ms = 60
    settings.barge_in_echo_suppression = True
    root = settings.data_dir / 'benchmark'
    output = load(root / 'greeting.wav')
    caller = load(root / 'phone-number.wav')
    rng = np.random.default_rng(4271)
    length = 40000
    t = np.arange(length, dtype=np.float32) / 8000
    road = 240 * np.sin(2 * np.pi * 72 * t) + 140 * np.sin(2 * np.pi * 113 * t) + rng.normal(0, 100, length)
    echo = np.zeros(length, dtype=np.float32)
    delay = 2560
    echo[delay:delay + min(len(output), length - delay)] = .65 * output[:length - delay]
    voice = np.zeros(length, dtype=np.float32)
    onset = 6400
    voice[onset:onset + min(len(caller), length - onset)] = caller[:length - onset]
    clunks = road.copy()
    for start in range(1200, length - 500, 5000):
        clunks[start:start + 240] += rng.normal(0, 2600, 240)
    results = []
    for label, incoming, interrupt in [
        ('road-rumble-and-hiss', road, False), ('short-clunks', clunks, False),
        ('speakerphone-echo', echo + road * .3, False),
        ('distorted-speakerphone-echo', 2200 * np.tanh(echo / 2200) + road * .3, False),
        ('caller-in-road-noise', voice + road, True),
        ('caller-over-speakerphone-echo', voice + echo + road * .3, True),
        ('caller-over-distorted-speakerphone-echo', voice + 2200 * np.tanh(echo / 2200) + road * .3, True),
    ]:
        call = AudioSocketConnection(asyncio.StreamReader(), Writer(), settings)
        began = time.perf_counter()
        triggered = None
        for offset in range(0, length, 160):
            frame = output[offset:offset + 160] if offset < len(output) else np.zeros(160)
            call._echo_filter.record(frame.astype('<i2').tobytes())
            raw = np.clip(incoming[offset:offset + 160], -32768, 32767).astype('<i2').tobytes()
            call._capture_barge_in(call._echo_filter.filter(raw))
            if call.barge_in_event.is_set():
                triggered = offset / 8 + 20
                break
        row = {'case': label, 'interrupted': triggered is not None, 'media_trigger_ms': triggered,
               'compute_ms': round((time.perf_counter() - began) * 1000, 2)}
        results.append(row)
        assert row['interrupted'] == interrupt, row
        if interrupt:
            assert 800 <= triggered <= 2400, row
    report = {'noise_interruption_check': 'passed', 'carrier_calls': 0, 'staff_messages': 0, 'uses_customer_audio': False, 'cases': results}
    (root / 'noise-interruption-report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
