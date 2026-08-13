from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_full_image_build_is_cpu_portable(project_root: Path) -> None:
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    required = (
        "-DGGML_NATIVE=OFF",
        "-DGGML_SSE42=OFF",
        "-DGGML_AVX=OFF",
        "-DGGML_AVX2=OFF",
        "-DGGML_BMI2=OFF",
        "-DGGML_FMA=OFF",
        "-DGGML_F16C=OFF",
        "--no-binary=llama-cpp-python",
        "-march=x86-64",
        "-mtune=generic",
    )
    for value in required:
        assert value in dockerfile
    assert "-DGGML_NATIVE=ON" not in dockerfile


def test_renderer_treats_gate_termination_as_nonfatal(
    project_root: Path, tmp_path: Path
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "runtime"),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "runtime/asterisk/etc"),
            "ASTERISK_MODULE_DIR": str(tmp_path / "modules"),
            "SIP_TRUNK_MODE": "disabled",
            "ARI_SECRET": "ari-test-secret",
            "AMI_SECRET": "ami-test-secret",
        }
    )
    subprocess.run(
        ["python", str(project_root / "scripts/render_asterisk.py")],
        check=True,
        env=env,
        cwd=project_root,
    )
    runtime = tmp_path / "runtime/asterisk"
    extensions = (runtime / "etc/extensions.conf").read_text(encoding="utf-8")
    assert "TryExec(AudioSocket(${FLOODMAN_GATE_UUID}" in extensions
    assert "Floodman gate AudioSocket completed with ${TRYSTATUS}" in extensions
    assert "\n same => n,AudioSocket(${FLOODMAN_GATE_UUID}" not in extensions
    assert (runtime / "logs/cdr-custom").is_dir()
