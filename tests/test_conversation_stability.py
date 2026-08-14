from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_production_disables_unstable_talkdetect(
    project_root: Path,
) -> None:
    module = load_module(
        "conversation_stability_config",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.production_patch()
    barge = patch["barge_in"]
    assert barge["enabled"] is False
    assert barge["pipeline_talk_detect_enabled"] is False
    assert barge["force_unmute"] is True
    assert barge["post_tts_end_protection_ms"] == 750


def test_groq_fast_intake_bounds_rate_limit_waits(
    project_root: Path,
) -> None:
    module = load_module(
        "groq_resilience_config",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    llm = module.production_patch()["pipelines"][
        "floodman_production"
    ]["options"]["llm"]
    assert llm["service_tier"] == (
        "${GROQ_SERVICE_TIER:-on_demand}"
    )
    assert llm["rate_limit_retries"] == (
        "${GROQ_RATE_LIMIT_RETRIES:-1}"
    )
    assert llm["rate_limit_max_wait_sec"] == (
        "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}"
    )


def test_image_applies_groq_resilience_patch(
    project_root: Path,
) -> None:
    patcher = (
        project_root / "scripts/patch_ava_groq_resilience.py"
    ).read_text(encoding="utf-8")
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "Floodman Groq 429 backoff patch" in patcher
    assert "response.status == 429" in patcher
    assert "Retry-After" in patcher
    assert "reset_after" in patcher
    assert "await asyncio.sleep(delay)" in patcher
    assert "COPY scripts/patch_ava_groq_resilience.py" in dockerfile
    assert "patch_ava_groq_resilience.py --ava-root /opt/ava" in dockerfile
    assert (
        'grep -q "Floodman Groq 429 backoff patch"'
        in dockerfile
    )
