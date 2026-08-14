from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("production_ai_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_profile_selector_contains_cloud_and_local_paths(
    project_root: Path,
) -> None:
    script = (
        project_root / "scripts/select_ai_profile.sh"
    ).read_text(encoding="utf-8")
    assert "production_hybrid" in script
    assert "local_hybrid" in script
    assert "DEEPGRAM_API_KEY" in script
    assert "GROQ_API_KEY" in script
    assert "ELEVENLABS_API_KEY" in script


def test_production_overlay_builds_expected_pipeline(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(
        project_root / "scripts/apply_production_hybrid_ai.py"
    )
    target = tmp_path / "ai-agent.local.yaml"
    target.write_text(
        "pipelines:\n  local_hybrid:\n    stt: local_stt\n",
        encoding="utf-8",
    )
    module.update(target, "production_hybrid")
    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))

    pipeline = parsed["pipelines"]["floodman_production"]
    assert pipeline["stt"] == "deepgram_flux_stt"
    assert pipeline["llm"] == "groq_llm"
    assert pipeline["tts"] == "elevenlabs_tts"
    assert pipeline["options"]["stt"]["streaming"] is True
    assert parsed["providers"]["deepgram"]["enabled"] is True
    assert parsed["providers"]["elevenlabs_tts"]["enabled"] is True


def test_docker_build_applies_production_ava_patches(
    project_root: Path,
) -> None:
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    assert "patch_ava_production.py" in dockerfile
    assert "Floodman Groq reasoning controls patch" in dockerfile


def test_scripts_have_valid_syntax(project_root: Path) -> None:
    subprocess.run(
        ["bash", "-n", str(project_root / "scripts/select_ai_profile.sh")],
        check=True,
        cwd=project_root,
    )
    for path in (
        project_root / "scripts/apply_production_hybrid_ai.py",
        project_root / "scripts/patch_ava_production.py",
    ):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
