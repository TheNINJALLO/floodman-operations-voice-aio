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
        # Production modular STT/LLM/TTS uses a dedicated full-duplex
        # Asterisk ExternalMedia leg. This keeps caller capture independent
        # from the greeting and response playback stream.
        "audio_transport": "externalmedia",
        "external_media": {
            "rtp_host": "${FLOODMAN_EXTERNALMEDIA_RTP_HOST:-127.0.0.1}",
            "advertise_host": "${FLOODMAN_EXTERNALMEDIA_ADVERTISE_HOST:-127.0.0.1}",
            "rtp_port": "${FLOODMAN_EXTERNALMEDIA_RTP_PORT:-18080}",
            "port_range": "${FLOODMAN_EXTERNALMEDIA_PORT_RANGE:-18080:18099}",
            "codec": "ulaw",
            "direction": "both",
            "format": "slin16",
            "sample_rate": 16000,
            "allowed_remote_hosts": ["127.0.0.1"],
            "lock_remote_endpoint": True,
        },
        "downstream_mode": "stream",
        # Balanced low-latency playback. The upstream 950 ms jitter buffer made
        # short answers feel disconnected even when provider generation was fast.
        "streaming": {
            "jitter_buffer_ms": "${FLOODMAN_STREAMING_JITTER_BUFFER_MS:-80}",
            "min_start_ms": "${FLOODMAN_STREAMING_MIN_START_MS:-25}",
            "greeting_min_start_ms": "${FLOODMAN_STREAMING_GREETING_MIN_START_MS:-20}",
            "low_watermark_ms": "${FLOODMAN_STREAMING_LOW_WATERMARK_MS:-40}",
            "provider_grace_ms": "${FLOODMAN_STREAMING_PROVIDER_GRACE_MS:-40}",
        },
        "profiles": {
            "telephony_enhanced_8k": {
                "idle_cutoff_ms": "${FLOODMAN_TTS_IDLE_CUTOFF_MS:-220}",
            },
        },
        # TALK_DETECT currently emits a stale ChannelTalkingStarted
        # event as response playback ends. Keep production calls
        # turn-based until barge-in is proven stable.
        "barge_in": {
            "enabled": False,
            "pipeline_talk_detect_enabled": False,
            "mode": "stop",
            "mute_ms": 120,
            "force_unmute": True,
            "post_tts_end_protection_ms": "${FLOODMAN_POST_TTS_END_PROTECTION_MS:-120}",
        },
        "no_input": {
            "enabled": True,
            "inbound_enabled": True,
            "outbound_enabled": True,
            "initial_timeout_sec": 12,
            "grace_timeout_sec": 8,
            "max_check_ins": 1,
            "check_in_message": "I didn't quite catch that. Could you say that again?",
            "final_message": "I'm still not hearing you, so I'll end the call now. Please call us back when you're ready. Goodbye.",
        },
        "pipelines": {
            # AVA upstream ships demo pipelines that Floodman does not use.
            # Null values are deletion markers in AVA local overrides.
            "local_hybrid": None,
            "local_hybrid_groq": None,
            "hybrid_elevenlabs": None,
            "floodman_production": {
                "stt": "deepgram_flux_stt",
                "llm": "groq_llm",
                "tts": "elevenlabs_tts",
                "options": {
                    "stt": {
                        "base_url": "wss://api.deepgram.com/v2/listen",
                        "model": "${DEEPGRAM_FLUX_MODEL:-flux-general-en}",
                        "encoding": "linear16",
                        "sample_rate": 16000,
                        "channels": 1,
                        "streaming": True,
                        "stream_format": "pcm16_16k",
                        "eot_threshold": "${DEEPGRAM_EOT_THRESHOLD:-0.82}",
                        "eager_eot_threshold": "${DEEPGRAM_EAGER_EOT_THRESHOLD:-0.70}",
                        "eot_timeout_ms": "${DEEPGRAM_EOT_TIMEOUT_MS:-1500}",
                    },
                    "llm": {
                        "chat_base_url": "https://api.groq.com/openai/v1",
                        "model": "${GROQ_LLM_MODEL:-qwen/qwen3.6-27b}",
                        "temperature": "${GROQ_LLM_TEMPERATURE:-0.10}",
                        "top_p": "${GROQ_LLM_TOP_P:-0.70}",
                        "presence_penalty": "${GROQ_LLM_PRESENCE_PENALTY:-0.2}",
                        "max_tokens": "${GROQ_LLM_MAX_TOKENS:-160}",
                        "timeout_sec": "${GROQ_LLM_TIMEOUT_SECONDS:-8}",
                        "reasoning_effort": "none",
                        "reasoning_format": "hidden",
                        "service_tier": "${GROQ_SERVICE_TIER:-on_demand}",
                        "rate_limit_retries": "${GROQ_RATE_LIMIT_RETRIES:-1}",
                        "rate_limit_max_wait_sec": "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}",
                        "rate_limit_fallback_model": "${GROQ_RATE_LIMIT_FALLBACK_MODEL:-llama-3.3-70b-versatile}",
                        "aggregation_timeout_sec": "${GROQ_AGGREGATION_TIMEOUT_SECONDS:-0.15}",
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
                "eot_threshold": 0.82,
                "eager_eot_threshold": 0.70,
                "eot_timeout_ms": 1500,
                "continuous_input": True,
            },
            "groq_llm": {
                "type": "openai",
                "enabled": True,
                "capabilities": ["llm"],
                "api_key": "${GROQ_API_KEY:-}",
                "chat_base_url": "https://api.groq.com/openai/v1",
                "chat_model": "${GROQ_LLM_MODEL:-qwen/qwen3.6-27b}",
                "response_timeout_sec": 8,
                "temperature": 0.10,
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
        # Preserve the existing local recovery stack on AudioSocket.
        "audio_transport": "audiosocket",
        "audiosocket": {
            "host": "127.0.0.1",
            "advertise_host": "127.0.0.1",
            "port": 8090,
            "format": "slin",
        },
        "downstream_mode": "stream",
        "pipelines": {
            # Production deliberately writes local_hybrid: null while cloud
            # providers are active. Recreate it explicitly on fallback.
            "local_hybrid": {
                "llm": "local_llm",
            },
            "floodman_production": None,
            "local_hybrid_groq": None,
            "hybrid_elevenlabs": None,
        },
        "providers": {
            "local_llm": {
                "enabled": True,
                "ws_url": "${LOCAL_WS_URL:-ws://127.0.0.1:8765}",
                "auth_token": "${LOCAL_WS_AUTH_TOKEN:-}",
            },
        },
    }


def validate_selected_profile(
    config: dict[str, Any],
    profile: str,
    *,
    path: Path,
) -> None:
    selected = (
        "floodman_production"
        if profile == "production_hybrid"
        else "local_hybrid"
    )
    pipelines = config.get("pipelines")
    if not isinstance(pipelines, dict):
        raise RuntimeError(f"{path}: pipelines must be a mapping")

    selected_config = pipelines.get(selected)
    if not isinstance(selected_config, dict):
        raise RuntimeError(
            f"{path}: selected pipeline {selected!r} is missing "
            "or was deleted"
        )

    if profile == "local_hybrid":
        providers = config.get("providers")
        local_provider = (
            providers.get("local_llm")
            if isinstance(providers, dict)
            else None
        )
        if not isinstance(local_provider, dict):
            raise RuntimeError(
                f"{path}: local_hybrid requires provider 'local_llm'"
            )
        if not local_provider.get("enabled"):
            raise RuntimeError(
                f"{path}: local_llm provider must be enabled"
            )


def update(path: Path, profile: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(current, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    patch = production_patch() if profile == "production_hybrid" else local_patch()
    merged = deep_merge(current, patch)
    validate_selected_profile(
        merged,
        profile,
        path=path,
    )
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
