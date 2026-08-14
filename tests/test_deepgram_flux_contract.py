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


def test_preflight_does_not_send_legacy_flux_language(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/validate_production_ai.py"
    ).read_text(encoding="utf-8")
    assert '"language": "en-US"' not in source
    assert "flux-general-en" in source
    assert "eot_timeout_ms" in source


def test_production_pipeline_does_not_force_language_query(
    project_root: Path,
) -> None:
    module = load_module(
        "production_ai_config_flux_test",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    stt = module.production_patch()["pipelines"][
        "floodman_production"
    ]["options"]["stt"]
    assert stt["model"] == "${DEEPGRAM_FLUX_MODEL:-flux-general-en}"
    assert "language" not in stt


def test_docker_applies_and_verifies_flux_patch(
    project_root: Path,
) -> None:
    patcher = (
        project_root / "scripts/patch_ava_flux.py"
    ).read_text(encoding="utf-8")
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "Floodman Flux v2 query contract patch" in patcher
    assert "language_hint" in patcher
    assert "COPY scripts/patch_ava_flux.py" in dockerfile
    assert "patch_ava_flux.py --ava-root /opt/ava" in dockerfile
    assert (
        'grep -q "Floodman Flux v2 query contract patch"'
        in dockerfile
    )
