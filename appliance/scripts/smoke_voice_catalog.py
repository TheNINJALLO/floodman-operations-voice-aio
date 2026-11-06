#!/usr/bin/env python3
"""Render and recognize each installed preset on CPU, with no carrier calls."""
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time
import wave
import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_models import configure_runtime_cache


async def run(data):
    import numpy as np
    from app.config import Settings
    from app.pocket_tts import PocketTTS, VOICES
    from app.streaming_stt import StreamingLocalSTT
    settings = Settings.from_env()
    settings.tts_cache_enabled = False
    tts, stt = PocketTTS(settings), StreamingLocalSTT(settings)
    class NoFallback:
        async def synthesize(self, *_args, **_kwargs): raise RuntimeError('Unexpected voice fallback')
    tts.fallback = NoFallback()
    await tts.start()
    await stt.start()
    output = data / 'voice-catalog-acceptance'
    output.mkdir(parents=True, exist_ok=True)
    results = []
    text = "Hello. How can I help you today? Let's take this one step at a time. I can get these details to the right team."
    for voice in VOICES:
        first = None
        started = time.perf_counter()
        pcm = bytearray()
        async for chunk in tts.stream_pcm(text, voice=voice, speed=1.0):
            if first is None: first = (time.perf_counter() - started) * 1000
            pcm.extend(chunk)
        pcm = bytes(pcm)
        with wave.open(str(output / (voice + '.wav')), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(pcm)
        async def frames():
            for offset in range(0, len(pcm), 320): yield pcm[offset:offset + 320]
            for _ in range(90): yield bytes(320)
        transcript = ' '.join([turn.text async for turn in stt.stream_turns(frames(), get_stage=lambda: 'name')])
        words = re.sub(r'[^a-z]', '', transcript.lower())
        rms = float(np.sqrt(np.mean(np.frombuffer(pcm, dtype='<i2').astype(np.float64) ** 2))) if pcm else 0
        item = {'voice': voice, 'first_audio_ms': round(first or 0, 2), 'audio_seconds': len(pcm) / 16000,
                'rms': round(rms, 2), 'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                'recognized_synthetic_text': transcript,
                'passed': rms > 100 and 2 < len(pcm) / 16000 < 25 and all(word in words for word in ['help', 'today', 'team'])}
        results.append(item)
        print(json.dumps(item), flush=True)
    report = {'passed': all(r['passed'] for r in results) and len({r['pcm_sha256'] for r in results}) == len(VOICES),
              'voice_count': len(results), 'voices': results, 'carrier_calls': 0, 'cloud_ai_requests': 0, 'uses_customer_data': False}
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if not report['passed']: raise SystemExit('Voice catalog acceptance needs review')


if __name__ == '__main__':
    data = Path(os.environ['DATA_DIR']).resolve()
    configure_runtime_cache(data)
    os.environ.update(LOCAL_VOICE_STACK='streaming_trial', PRIMARY_MODEL_PROFILE='haiku8b',
                      LOCAL_AI_ONLY='true', LOCAL_AI_DEVICE='cpu', SHERPA_WHISPER_FALLBACK='true', HF_HUB_OFFLINE='1')
    asyncio.run(run(data))
