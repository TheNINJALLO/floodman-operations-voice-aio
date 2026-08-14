from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_managed_runtime_sync_replaces_stale_floodman_tools(
    project_root: Path,
    tmp_path: Path,
) -> None:
    module = load_module(
        "sync_fast_intake_runtime_test",
        project_root / "scripts/sync_fast_intake_runtime.py",
    )
    canonical = tmp_path / "canonical.yaml"
    target = tmp_path / "target.yaml"
    canonical.write_text(
        """
llm:
  initial_greeting: "New managed greeting"
no_input:
  enabled: true
  initial_timeout_sec: 8
in_call_tools:
  floodman_submit_intake:
    enabled: true
    is_global: false
  floodman_schedule_inspection:
    enabled: false
    is_global: false
  floodman_check_availability:
    enabled: false
    is_global: false
  floodman_reschedule_inspection:
    enabled: false
    is_global: false
""".lstrip(),
        encoding="utf-8",
    )
    target.write_text(
        """
llm:
  initial_greeting: "Old greeting"
in_call_tools:
  custom_operator_tool:
    enabled: true
  floodman_schedule_inspection:
    enabled: true
    is_global: true
""".lstrip(),
        encoding="utf-8",
    )

    assert module.sync_managed_config(canonical, target) is True
    assert module.sync_managed_config(canonical, target) is False

    result = yaml.safe_load(target.read_text(encoding="utf-8"))
    tools = result["in_call_tools"]
    assert tools["custom_operator_tool"]["enabled"] is True
    assert tools["floodman_submit_intake"]["enabled"] is True
    assert tools["floodman_schedule_inspection"]["enabled"] is False
    assert tools["floodman_check_availability"]["enabled"] is False
    assert tools["floodman_reschedule_inspection"]["enabled"] is False
    assert result["llm"]["initial_greeting"] == "New managed greeting"


def test_entrypoint_syncs_tools_and_uses_asterisk_sound_directory(
    project_root: Path,
) -> None:
    entrypoint = (
        project_root / "scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "sync_fast_intake_runtime.py" in entrypoint
    assert (
        'AST_MEDIA_DIR="${AST_MEDIA_DIR:-${DATA_DIR}/asterisk/sounds/ai-generated}"'
        in entrypoint
    )
    assert 'mkdir -p "${AST_MEDIA_DIR}"' in entrypoint


def test_production_pipeline_has_immediate_groq_model_fallback(
    project_root: Path,
) -> None:
    module = load_module(
        "fast_intake_live_production_patch",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.production_patch()
    llm = patch["pipelines"]["floodman_production"]["options"]["llm"]
    assert llm["rate_limit_fallback_model"] == (
        "${GROQ_RATE_LIMIT_FALLBACK_MODEL:-llama-3.3-70b-versatile}"
    )
    assert patch["no_input"]["initial_timeout_sec"] == 12
    assert patch["no_input"]["grace_timeout_sec"] == 8


def test_image_applies_groq_model_fallback_patch(
    project_root: Path,
) -> None:
    patcher = (
        project_root / "scripts/patch_ava_groq_model_fallback.py"
    ).read_text(encoding="utf-8")
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "Floodman Groq model fallback patch" in patcher
    assert "rate_limit_fallback_model" in patcher
    assert "switching to fallback model" in patcher
    assert "COPY scripts/patch_ava_groq_model_fallback.py" in dockerfile
    assert "patch_ava_groq_model_fallback.py --ava-root /opt/ava" in dockerfile
    assert 'grep -q "Floodman Groq model fallback patch"' in dockerfile


def test_canonical_fast_intake_tool_remains_managed(
    project_root: Path,
) -> None:
    overlay = yaml.safe_load(
        (
            project_root / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    tools = overlay["in_call_tools"]
    assert tools["floodman_submit_intake"]["enabled"] is True
    assert tools["floodman_schedule_inspection"]["enabled"] is False
    assert tools["floodman_check_availability"]["enabled"] is False
    assert tools["floodman_reschedule_inspection"]["enabled"] is False
