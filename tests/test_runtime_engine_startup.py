from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_local_ai_launcher_uses_sibling_import_compatible_mode(project_root: Path) -> None:
    script = (project_root / "scripts/run_local_ai.sh").read_text(encoding="utf-8")
    assert 'LOCAL_AI_DIR="${AVA_RUNTIME_DIR}/local_ai_server"' in script
    assert 'cd "${AVA_RUNTIME_DIR}"' in script
    assert '"${LOCAL_AI_DIR}/main.py"' in script
    assert "-m local_ai_server.main" not in script
    assert 'PYTHONPATH="${LOCAL_AI_DIR}:${AVA_RUNTIME_DIR}' in script


def test_entrypoint_redirects_read_only_image_caches(project_root: Path) -> None:
    script = (project_root / "scripts/entrypoint.sh").read_text(encoding="utf-8")
    assert "Floodman writable runtime cache normalization" in script
    assert '""|/opt/model-cache|/opt/model-cache/*)' in script
    assert '"${DATA_DIR}/model-cache/huggingface"' in script
    assert '"${DATA_DIR}/cache"' in script
    assert "HF_HUB_CACHE" in script
    assert "HUGGINGFACE_HUB_CACHE" in script


def test_asterisk_renderer_uses_packaged_data_and_stasis_config(
    project_root: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    config_dir = data_dir / "asterisk/etc"
    module_dir = tmp_path / "modules"
    package_data = tmp_path / "usr-share-asterisk"
    module_dir.mkdir(parents=True)
    docs = package_data / "documentation"
    docs.mkdir(parents=True)
    (docs / "core-en_US.xml").write_text("<docs/>\n", encoding="utf-8")

    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "ASTERISK_CONFIG_DIR": str(config_dir),
        "ASTERISK_MODULE_DIR": str(module_dir),
        "ASTERISK_DATA_DIR": str(package_data),
        "SIP_TRUNK_MODE": "disabled",
        "ARI_SECRET": "test-ari-secret",
        "AMI_SECRET": "test-ami-secret",
        "CALL_RECORDING_ENABLED": "false",
    }
    result = subprocess.run(
        [sys.executable, str(project_root / "scripts/render_asterisk.py")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    asterisk_conf = (config_dir / "asterisk.conf").read_text(encoding="utf-8")
    assert f"astdatadir => {package_data}" in asterisk_conf
    assert f"astvarlibdir => {data_dir / 'asterisk/varlib'}" in asterisk_conf

    stasis_conf = (config_dir / "stasis.conf").read_text(encoding="utf-8")
    assert "[threadpool]" in stasis_conf
    assert "initial_size=5" in stasis_conf
    assert "max_size=50" in stasis_conf


def test_asterisk_renderer_fails_closed_without_core_docs(
    project_root: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    module_dir = tmp_path / "modules"
    package_data = tmp_path / "missing-asterisk-data"
    module_dir.mkdir(parents=True)
    package_data.mkdir(parents=True)
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "ASTERISK_CONFIG_DIR": str(data_dir / "asterisk/etc"),
        "ASTERISK_MODULE_DIR": str(module_dir),
        "ASTERISK_DATA_DIR": str(package_data),
        "SIP_TRUNK_MODE": "disabled",
        "ARI_SECRET": "test-ari-secret",
        "AMI_SECRET": "test-ami-secret",
    }
    result = subprocess.run(
        [sys.executable, str(project_root / "scripts/render_asterisk.py")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "core-en_US.xml" in result.stderr
