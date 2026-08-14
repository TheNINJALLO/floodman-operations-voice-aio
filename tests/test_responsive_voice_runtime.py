from __future__ import annotations

import importlib.util
import json
import yaml
import sys
from pathlib import Path
from types import SimpleNamespace

from app.call_gate.audio_socket import opening_silence_timeout


def _load_voice_module(project_root: Path):
    path = project_root / "scripts/prepare_floodman_voice.py"
    spec = importlib.util.spec_from_file_location(
        "prepare_floodman_voice",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclasses with postponed annotations inspect sys.modules while
    # the class is being created. Register the dynamic module before
    # executing it, matching Python's normal import machinery.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_direct_and_google_opening_windows_are_separate() -> None:
    settings = SimpleNamespace(
        gate_no_speech_timeout_seconds=1.4,
        gate_max_seconds=11.0,
    )
    assert opening_silence_timeout(settings, "") == 1.4
    assert opening_silence_timeout(settings, "direct") == 1.4
    assert opening_silence_timeout(settings, "Google LSA") == 11.0


def test_entrypoint_uses_telephone_latency_defaults(
    project_root: Path,
) -> None:
    entrypoint = (
        project_root / "scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "FLOODMAN_LOW_LATENCY_MODE" in entrypoint
    assert 'LOCAL_STT_IDLE_MS:-700' in entrypoint
    assert 'LOCAL_LLM_CHAT_FORMAT:-auto' in entrypoint
    assert 'LOCAL_LLM_CONTEXT:-2048' in entrypoint
    assert "LOCAL_ENABLE_FILLER_AUDIO" in entrypoint
    assert "LOCAL_TTS_PHRASE_CACHE_ENABLED" in entrypoint
    assert "prepare_floodman_voice.py" in entrypoint
    assert "2026-08-13.2" in entrypoint


def test_official_voice_profiles_are_pinned_and_checksummed(
    project_root: Path,
) -> None:
    module = _load_voice_module(project_root)
    assert set(module.PROFILES) == {
        "warm_female",
        "clear_female",
        "warm_male",
    }
    for profile in module.PROFILES.values():
        assert profile.model_url.startswith(
            "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
        )
        assert len(profile.sha256) == 64
        int(profile.sha256, 16)
    assert module.normalize_profile("female") == "warm_female"
    assert module.normalize_profile("ryan-high") == "warm_male"


def test_managed_greeting_is_natural_but_disclosed(
    project_root: Path,
) -> None:
    agents = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    overlay = yaml.safe_load(
        (
            project_root / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    greeting = (
        "Thanks for calling Floodman. This is Ava, the automated "
        "assistant. How can I help?"
    )
    assert greeting in agents
    assert isinstance(overlay, dict)
    assert overlay["llm"]["initial_greeting"] == greeting
    assert "automated assistant" in greeting.lower()



def test_eggs_expose_responsiveness_controls(
    project_root: Path,
) -> None:
    for relative in (
        "pterodactyl/egg-floodman-operations-voice-aio.json",
        "egg-floodman-operations-voice-aio-v1.1.1.json",
    ):
        path = project_root / relative
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        by_env = {
            item["env_variable"]: item
            for item in data.get("variables", [])
        }
        assert (
            by_env["FLOODMAN_DIRECT_GREETING_DELAY_SECONDS"][
                "default_value"
            ]
            == "1.4"
        )
        assert (
            by_env["FLOODMAN_VOICE_PROFILE"]["default_value"]
            == "warm_female"
        )
        assert by_env["LOCAL_STT_IDLE_MS"]["default_value"] == "700"
