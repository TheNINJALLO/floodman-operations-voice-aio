#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml


def deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def production_patch() -> dict[str, Any]:
    return {
        "default_provider": "${AVA_PROVIDER:-floodman_production}",
        "active_pipeline": "${AVA_PIPELINE:-floodman_production}",
        "pipelines": {
            "floodman_production": {
                "stt": "deepgram_flux_stt",
                "llm": "groq_llm",
                "tts": "elevenlabs_tts",
                "options": {
                    "stt": {
                        "base_url": "wss://api.deepgram.com/v2/listen",
                        "model": "${DEEPGRAM_FLUX_MODEL:-flux-general-en}",
                        "language": "en-US",
                        "encoding": "linear16",
                        "sample_rate": 16000,
                        "channels": 1,
                        "streaming": True,
                        "stream_format": "pcm16_16k",
                        "eot_threshold": "${DEEPGRAM_EOT_THRESHOLD:-0.70}",
                        "eager_eot_threshold": "${DEEPGRAM_EAGER_EOT_THRESHOLD:-0.50}",
                        "eot_timeout_ms": "${DEEPGRAM_EOT_TIMEOUT_MS:-1200}",
                    },
                    "llm": {
                        "chat_base_url": "https://api.groq.com/openai/v1",
                        "model": "${GROQ_LLM_MODEL:-qwen/qwen3.6-27b}",
                        "temperature": "${GROQ_LLM_TEMPERATURE:-0.65}",
                        "top_p": "${GROQ_LLM_TOP_P:-0.80}",
                        "presence_penalty": "${GROQ_LLM_PRESENCE_PENALTY:-1.0}",
                        "max_tokens": "${GROQ_LLM_MAX_TOKENS:-160}",
                        "timeout_sec": "${GROQ_LLM_TIMEOUT_SECONDS:-12}",
                        "reasoning_effort": "none",
                        "reasoning_format": "hidden",
                        "service_tier": "${GROQ_SERVICE_TIER:-auto}",
                    },
                    "tts": {
                        "voice_id": "${ELEVENLABS_VOICE_ID:-21m00Tcm4TlvDq8ikWAM}",
                        "model_id": "${ELEVENLABS_TTS_MODEL:-eleven_flash_v2_5}",
                        "output_format": "ulaw_8000",
                        "stability": "${ELEVENLABS_STABILITY:-0.45}",
                        "similarity_boost": "${ELEVENLABS_SIMILARITY_BOOST:-0.85}",
                        "style": "${ELEVENLABS_STYLE:-0.15}",
                        "use_speaker_boost": True,
                        "chunk_size_ms": 20,
                        "format": {"encoding": "mulaw", "sample_rate": 8000},
                    },
                },
            },
        },
        "providers": {
            "deepgram": {
                "type": "full",
                "enabled": True,
                "capabilities": ["stt", "llm", "tts"],
                "api_key": "${DEEPGRAM_API_KEY:-}",
                "base_url": "wss://api.deepgram.com/v2/listen",
                "model": "${DEEPGRAM_FLUX_MODEL:-flux-general-en}",
                "stt_language": "en-US",
                "eot_threshold": 0.70,
                "eager_eot_threshold": 0.50,
                "eot_timeout_ms": 1200,
                "continuous_input": True,
            },
            "groq_llm": {
                "type": "openai",
                "enabled": True,
                "capabilities": ["llm"],
                "api_key": "${GROQ_API_KEY:-}",
                "chat_base_url": "https://api.groq.com/openai/v1",
                "chat_model": "${GROQ_LLM_MODEL:-qwen/qwen3.6-27b}",
                "response_timeout_sec": 12,
                "temperature": 0.65,
                "tools_enabled": True,
            },
            "elevenlabs_tts": {
                "type": "elevenlabs",
                "enabled": True,
                "capabilities": ["tts"],
                "api_key": "${ELEVENLABS_API_KEY:-}",
                "voice_id": "${ELEVENLABS_VOICE_ID:-21m00Tcm4TlvDq8ikWAM}",
                "model_id": "${ELEVENLABS_TTS_MODEL:-eleven_flash_v2_5}",
                "base_url": "https://api.elevenlabs.io/v1",
                "output_format": "ulaw_8000",
                "stability": 0.45,
                "similarity_boost": 0.85,
                "style": 0.15,
                "use_speaker_boost": True,
            },
        },
    }


def local_patch() -> dict[str, Any]:
    return {
        "default_provider": "${AVA_PROVIDER:-local_hybrid}",
        "active_pipeline": "${AVA_PIPELINE:-local_hybrid}",
    }


def update(path: Path, profile: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(current, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    patch = production_patch() if profile == "production_hybrid" else local_patch()
    merged = deep_merge(current, patch)
    path.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument(
        "--profile",
        choices=("production_hybrid", "local_hybrid"),
        default=os.getenv("FLOODMAN_AI_PROFILE", "local_hybrid"),
    )
    args = parser.parse_args()
    for path in args.config:
        update(path, args.profile)
        print(f"Applied Floodman {args.profile} AI configuration to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
