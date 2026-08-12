from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _make_read_only(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_symlink():
            continue
        child.chmod(0o555 if child.is_dir() else 0o444)
    path.chmod(0o555)


def _build_fake_ava(image_root: Path) -> None:
    (image_root / "local_ai_server").mkdir(parents=True)
    (image_root / "config").mkdir()
    (image_root / "models").mkdir()
    (image_root / "scripts").mkdir()
    (image_root / ".git").mkdir()
    (image_root / "main.py").write_text("print('ava')\n", encoding="utf-8")
    (image_root / "local_ai_server/__init__.py").write_text("", encoding="utf-8")
    (image_root / "config/ai-agent.yaml").write_text("base: true\n", encoding="utf-8")
    (image_root / "models/registry.json").write_text('{"models": []}\n', encoding="utf-8")
    (image_root / "scripts/model_setup.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (image_root / ".git/config").write_text("ignored\n", encoding="utf-8")
    _make_read_only(image_root)


def test_prepare_ava_runtime_works_with_read_only_image(project_root: Path, tmp_path: Path) -> None:
    script = project_root / "scripts/prepare_ava_runtime.sh"
    image_root = tmp_path / "image-ava"
    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    local_config = config_dir / "ava/ai-agent.local.yaml"
    local_config.parent.mkdir(parents=True)
    local_config.write_text("floodman: first\n", encoding="utf-8")
    _build_fake_ava(image_root)

    runtime_dir = data_dir / "runtime/ava"
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "CONFIG_DIR": str(config_dir),
        "AVA_IMAGE_DIR": str(image_root),
        "AVA_RUNTIME_DIR": str(runtime_dir),
    }
    first = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    assert (runtime_dir / "main.py").is_file()
    assert not (runtime_dir / ".git").exists()
    assert (runtime_dir / "config/ai-agent.local.yaml").read_text() == "floodman: first\n"
    assert (runtime_dir / "models").is_symlink()
    assert (runtime_dir / "models").resolve() == (data_dir / "models").resolve()
    assert (data_dir / "models/registry.json").is_file()
    assert not (image_root / "config/ai-agent.local.yaml").exists()

    local_config.write_text("floodman: second\n", encoding="utf-8")
    second = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    assert (runtime_dir / "config/ai-agent.local.yaml").read_text() == "floodman: second\n"
    assert (runtime_dir / "models").resolve() == (data_dir / "models").resolve()


def test_prepare_ava_runtime_rejects_path_outside_data(project_root: Path, tmp_path: Path) -> None:
    script = project_root / "scripts/prepare_ava_runtime.sh"
    image_root = tmp_path / "image-ava"
    data_dir = tmp_path / "data"
    _build_fake_ava(image_root)
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "CONFIG_DIR": str(data_dir / "config"),
        "AVA_IMAGE_DIR": str(image_root),
        "AVA_RUNTIME_DIR": str(tmp_path / "outside-ava"),
    }
    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "must be a child of DATA_DIR" in result.stderr


def test_startup_never_mutates_image_ava_tree(project_root: Path) -> None:
    entrypoint = (project_root / "scripts/entrypoint.sh").read_text(encoding="utf-8")
    assert "prepare_ava_runtime.sh" in entrypoint
    assert 'cp "${CONFIG_DIR}/ava/ai-agent.local.yaml" /opt/ava' not in entrypoint
    assert "rm -rf /opt/ava/models" not in entrypoint
    assert "/opt/ava/scripts/model_setup.sh" not in entrypoint
    assert "agi_record_finalize.py" in entrypoint

    run_ava = (project_root / "scripts/run_ava.sh").read_text(encoding="utf-8")
    run_local = (project_root / "scripts/run_local_ai.sh").read_text(encoding="utf-8")
    supervisor = (project_root / "supervisord.conf").read_text(encoding="utf-8")
    assert 'cd "${AVA_RUNTIME_DIR}"' in run_ava
    assert 'cd "${AVA_RUNTIME_DIR}"' in run_local
    assert "directory=%(ENV_AVA_RUNTIME_DIR)s" in supervisor
