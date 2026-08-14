from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_profile_module(project_root: Path):
    path = project_root / "scripts/apply_production_hybrid_ai.py"
    spec = importlib.util.spec_from_file_location(
        "floodman_profile_pipeline_pruning", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def merge_with_null_deletion(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_with_null_deletion(merged[key], value)
        else:
            merged[key] = value
    return merged


def upstream_demo_pipelines() -> dict:
    return {
        "local_hybrid": {"stt": "local_stt", "tts": "local_tts"},
        "local_hybrid_groq": {
            "stt": "local_stt",
            "llm": "groq_llm",
            "tts": "local_tts",
        },
        "hybrid_elevenlabs": {
            "stt": "local_stt",
            "llm": "openai_llm",
            "tts": "elevenlabs_tts",
        },
    }


def test_production_profile_keeps_only_floodman_production(
    project_root: Path,
) -> None:
    module = load_profile_module(project_root)
    patch = module.production_patch()
    pipeline_patch = patch["pipelines"]
    assert pipeline_patch["local_hybrid"] is None
    assert pipeline_patch["local_hybrid_groq"] is None
    assert pipeline_patch["hybrid_elevenlabs"] is None

    merged = merge_with_null_deletion(
        {"pipelines": upstream_demo_pipelines()}, patch
    )
    assert set(merged["pipelines"]) == {"floodman_production"}


def test_local_profile_removes_stale_cloud_and_demo_pipelines(
    project_root: Path,
) -> None:
    module = load_profile_module(project_root)
    patch = module.local_patch()
    pipeline_patch = patch["pipelines"]
    assert pipeline_patch["floodman_production"] is None
    assert pipeline_patch["local_hybrid_groq"] is None
    assert pipeline_patch["hybrid_elevenlabs"] is None

    base = upstream_demo_pipelines()
    base["floodman_production"] = {
        "stt": "deepgram_flux_stt",
        "llm": "groq_llm",
        "tts": "elevenlabs_tts",
    }
    merged = merge_with_null_deletion({"pipelines": base}, patch)
    assert set(merged["pipelines"]) == {"local_hybrid"}
