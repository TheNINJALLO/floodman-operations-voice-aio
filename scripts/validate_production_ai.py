#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class ValidationError(RuntimeError):
    pass


def getenv(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(700).decode("utf-8", errors="replace")
    except Exception:
        return str(exc)


def json_request(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ValidationError(
            f"{url} returned HTTP {exc.code}: {read_http_error(exc)}"
        ) from exc
    except OSError as exc:
        raise ValidationError(f"{url} connection failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{url} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{url} returned an unexpected response")
    return value


async def validate_deepgram(api_key: str, model: str) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise ValidationError("websockets is not installed") from exc

    query = urllib.parse.urlencode(
        {
            "model": model,
            "language": "en-US",
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            "eot_threshold": getenv("DEEPGRAM_EOT_THRESHOLD", "0.70"),
            "eager_eot_threshold": getenv(
                "DEEPGRAM_EAGER_EOT_THRESHOLD", "0.50"
            ),
            "eot_timeout_ms": getenv("DEEPGRAM_EOT_TIMEOUT_MS", "1200"),
        }
    )
    url = f"wss://api.deepgram.com/v2/listen?{query}"
    try:
        async with websockets.connect(
            url,
            additional_headers=[
                ("Authorization", f"Token {api_key}"),
                ("User-Agent", "Floodman-Voice-AIO/provider-check"),
            ],
            open_timeout=12,
            close_timeout=3,
            ping_interval=None,
            max_size=2 * 1024 * 1024,
        ) as websocket:
            await websocket.send(b"\x00" * 3200)
            try:
                await asyncio.wait_for(websocket.recv(), timeout=1.5)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        raise ValidationError(f"Deepgram Flux failed: {exc}") from exc


def validate_groq(api_key: str, model: str) -> None:
    result = json_request(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        payload={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with the single word READY.",
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
        },
        timeout=25,
    )
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValidationError(f"Groq model {model!r} returned no choices")


def validate_elevenlabs(
    api_key: str,
    voice_id: str,
    model_id: str,
    *,
    audio_probe: bool,
) -> None:
    if not voice_id:
        raise ValidationError(
            "ELEVENLABS_VOICE_ID is empty. Copy one from My Voices."
        )

    base = "https://api.elevenlabs.io/v1"
    headers = {"xi-api-key": api_key}
    voice = json_request(
        f"{base}/voices/{urllib.parse.quote(voice_id, safe='')}",
        headers=headers,
        timeout=15,
    )
    if str(voice.get("voice_id") or "") != voice_id:
        raise ValidationError(
            f"ElevenLabs did not return voice {voice_id!r}"
        )
    if not audio_probe:
        return

    url = (
        f"{base}/text-to-speech/"
        f"{urllib.parse.quote(voice_id, safe='')}"
        "?output_format=ulaw_8000"
    )
    payload = {
        "text": "Ready.",
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.85,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/*",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            audio = response.read(4096)
    except urllib.error.HTTPError as exc:
        raise ValidationError(
            f"ElevenLabs TTS returned HTTP {exc.code}: "
            f"{read_http_error(exc)}"
        ) from exc
    except OSError as exc:
        raise ValidationError(
            f"ElevenLabs TTS connection failed: {exc}"
        ) from exc
    if len(audio) < 80:
        raise ValidationError(
            "ElevenLabs returned empty or truncated telephone audio"
        )


def write_marker(path: Path, details: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "validated_at": int(time.time()),
                **details,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--no-audio-probe", action="store_true")
    args = parser.parse_args()

    deepgram_key = getenv("DEEPGRAM_API_KEY")
    groq_key = getenv("GROQ_API_KEY")
    elevenlabs_key = getenv("ELEVENLABS_API_KEY")
    voice_id = getenv("ELEVENLABS_VOICE_ID")

    missing = [
        name
        for name, value in (
            ("DEEPGRAM_API_KEY", deepgram_key),
            ("GROQ_API_KEY", groq_key),
            ("ELEVENLABS_API_KEY", elevenlabs_key),
            ("ELEVENLABS_VOICE_ID", voice_id),
        )
        if not value
    ]
    if missing:
        args.marker.unlink(missing_ok=True)
        print(
            "Production AI validation failed: missing "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    deepgram_model = getenv(
        "DEEPGRAM_FLUX_MODEL", "flux-general-en"
    )
    groq_model = getenv(
        "GROQ_LLM_MODEL", "qwen/qwen3.6-27b"
    )
    elevenlabs_model = getenv(
        "ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"
    )

    try:
        asyncio.run(validate_deepgram(deepgram_key, deepgram_model))
        validate_groq(groq_key, groq_model)
        validate_elevenlabs(
            elevenlabs_key,
            voice_id,
            elevenlabs_model,
            audio_probe=not args.no_audio_probe,
        )
    except ValidationError as exc:
        args.marker.unlink(missing_ok=True)
        print(f"Production AI validation failed: {exc}", file=sys.stderr)
        return 1

    write_marker(
        args.marker,
        {
            "profile": "production_hybrid",
            "deepgram_model": deepgram_model,
            "groq_model": groq_model,
            "elevenlabs_model": elevenlabs_model,
            "elevenlabs_voice_id": voice_id,
        },
    )
    print(
        "Production AI validation passed: "
        "Deepgram, Groq, and ElevenLabs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
