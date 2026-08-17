from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml


def load_module(project_root: Path):
    path = project_root / "scripts/apply_production_hybrid_ai.py"
    spec = importlib.util.spec_from_file_location(
        "floodman_profile_fallback_recovery",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_config() -> dict:
    return {
        "default_provider": "${AVA_PROVIDER:-local_hybrid}",
        "active_pipeline": "${AVA_PIPELINE:-local_hybrid}",
        "pipelines": {
            "local_hybrid": {
                "llm": "local_llm",
            },
        },
        "providers": {
            "local_llm": {
                "enabled": True,
                "ws_url": "${LOCAL_WS_URL:-ws://127.0.0.1:8765}",
                "auth_token": "${LOCAL_WS_AUTH_TOKEN:-}",
            },
        },
    }


def test_local_patch_explicitly_recreates_pipeline(
    project_root: Path,
) -> None:
    module = load_module(project_root)
    patch = module.local_patch()

    assert patch["default_provider"] == (
        "${AVA_PROVIDER:-local_hybrid}"
    )
    assert patch["active_pipeline"] == (
        "${AVA_PIPELINE:-local_hybrid}"
    )
    assert patch["pipelines"]["local_hybrid"] == {
        "llm": "local_llm",
    }
    assert patch["pipelines"]["floodman_production"] is None
    assert patch["providers"]["local_llm"]["enabled"] is True


def test_production_to_local_transition_restores_deleted_pipeline(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(project_root)
    target = tmp_path / "ai-agent.local.yaml"
    target.write_text(
        yaml.safe_dump(canonical_config(), sort_keys=False),
        encoding="utf-8",
    )

    module.update(target, "production_hybrid")
    production = yaml.safe_load(
        target.read_text(encoding="utf-8")
    )
    assert production["pipelines"]["local_hybrid"] is None
    assert isinstance(
        production["pipelines"]["floodman_production"],
        dict,
    )

    module.update(target, "local_hybrid")
    recovered = yaml.safe_load(
        target.read_text(encoding="utf-8")
    )
    assert recovered["pipelines"]["local_hybrid"] == {
        "llm": "local_llm",
    }
    assert recovered["pipelines"]["floodman_production"] is None
    assert recovered["providers"]["local_llm"]["enabled"] is True
    module.validate_selected_profile(
        recovered,
        "local_hybrid",
        path=target,
    )


def test_stale_persistent_null_pipeline_self_heals(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(project_root)
    target = tmp_path / "stale-ai-agent.local.yaml"
    stale = canonical_config()
    stale["pipelines"]["local_hybrid"] = None
    stale["pipelines"]["floodman_production"] = {
        "stt": "deepgram_flux_stt",
        "llm": "groq_llm",
        "tts": "elevenlabs_tts",
    }
    target.write_text(
        yaml.safe_dump(stale, sort_keys=False),
        encoding="utf-8",
    )

    module.update(target, "local_hybrid")
    healed = yaml.safe_load(
        target.read_text(encoding="utf-8")
    )
    assert healed["pipelines"]["local_hybrid"] == {
        "llm": "local_llm",
    }
    assert healed["pipelines"]["floodman_production"] is None


def test_repeated_local_application_is_stable(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(project_root)
    target = tmp_path / "repeat-ai-agent.local.yaml"
    target.write_text(
        yaml.safe_dump(canonical_config(), sort_keys=False),
        encoding="utf-8",
    )

    module.update(target, "local_hybrid")
    first = target.read_text(encoding="utf-8")
    module.update(target, "local_hybrid")
    second = target.read_text(encoding="utf-8")
    assert second == first


def test_validator_rejects_the_runtime_failure_shape(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(project_root)
    broken = canonical_config()
    broken["pipelines"]["local_hybrid"] = None

    try:
        module.validate_selected_profile(
            broken,
            "local_hybrid",
            path=tmp_path / "broken.yaml",
        )
    except RuntimeError as exc:
        assert "local_hybrid" in str(exc)
        assert "missing or was deleted" in str(exc)
    else:
        raise AssertionError("broken local fallback was accepted")
