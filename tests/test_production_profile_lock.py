from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


CLOUD_KEYS = {
    "DEEPGRAM_API_KEY": "dg-test",
    "GROQ_API_KEY": "gsk-test",
    "ELEVENLABS_API_KEY": "el-test",
    "ELEVENLABS_VOICE_ID": "voice-test",
}


def _run_selector(
    project_root: Path,
    tmp_path: Path,
    *,
    requested: str,
    probe_exit: int = 0,
    strict: str = "false",
    include_keys: bool = True,
) -> subprocess.CompletedProcess[str]:
    validator = tmp_path / "fake_validator.py"
    validator.write_text(
        "import os, sys\n"
        "sys.exit(int(os.environ.get('FAKE_PROBE_EXIT', '0')))\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "FLOODMAN_AI_PROFILE": requested,
            "FLOODMAN_PRODUCTION_STRICT": strict,
            "FLOODMAN_CLOUD_AUDIO_PROBE": "false",
            "FLOODMAN_VALIDATOR_PYTHON": sys.executable,
            "FLOODMAN_PRODUCTION_VALIDATOR": str(validator),
            "FAKE_PROBE_EXIT": str(probe_exit),
        }
    )
    for name in CLOUD_KEYS:
        if include_keys:
            env[name] = CLOUD_KEYS[name]
        else:
            env.pop(name, None)

    script = project_root / "scripts/select_ai_profile.sh"
    command = r'''
source "$1"
printf 'PROFILE=%s\nPIPELINE=%s\nPROVIDER=%s\nREQUESTED=%s\n' \
  "$FLOODMAN_AI_PROFILE" \
  "$AVA_PIPELINE" \
  "$AVA_PROVIDER" \
  "$FLOODMAN_AI_PROFILE_REQUESTED"
'''
    return subprocess.run(
        ["bash", "-c", command, "bash", str(script)],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_explicit_production_success_stays_production(
    project_root: Path,
    tmp_path: Path,
) -> None:
    result = _run_selector(
        project_root,
        tmp_path,
        requested="production_hybrid",
        probe_exit=0,
    )
    assert result.returncode == 0, result.stderr
    assert "PROFILE=production_hybrid" in result.stdout
    assert "PIPELINE=floodman_production" in result.stdout
    assert "PROVIDER=floodman_production" in result.stdout
    assert "REQUESTED=production_hybrid" in result.stdout


def test_explicit_production_probe_failure_never_becomes_local(
    project_root: Path,
    tmp_path: Path,
) -> None:
    result = _run_selector(
        project_root,
        tmp_path,
        requested="production_hybrid",
        probe_exit=1,
        strict="false",
    )
    assert result.returncode != 0
    assert "will not fall back to local_hybrid" in result.stderr
    assert "PROFILE=local_hybrid" not in result.stdout


def test_explicit_production_missing_keys_never_becomes_local(
    project_root: Path,
    tmp_path: Path,
) -> None:
    result = _run_selector(
        project_root,
        tmp_path,
        requested="production_hybrid",
        include_keys=False,
        strict="false",
    )
    assert result.returncode != 0
    assert "missing DEEPGRAM_API_KEY" in result.stderr
    assert "PROFILE=local_hybrid" not in result.stdout


def test_auto_mode_can_still_use_repaired_local_fallback(
    project_root: Path,
    tmp_path: Path,
) -> None:
    result = _run_selector(
        project_root,
        tmp_path,
        requested="auto",
        probe_exit=1,
        strict="false",
    )
    assert result.returncode == 0, result.stderr
    assert "PROFILE=local_hybrid" in result.stdout
    assert "PIPELINE=local_hybrid" in result.stdout
    assert "REQUESTED=auto" in result.stdout
    assert "auto mode" in result.stderr


def test_explicit_local_still_selects_local(
    project_root: Path,
    tmp_path: Path,
) -> None:
    result = _run_selector(
        project_root,
        tmp_path,
        requested="local_hybrid",
        include_keys=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PROFILE=local_hybrid" in result.stdout
    assert "REQUESTED=local_hybrid" in result.stdout


def test_entrypoint_selects_profile_before_rendering_asterisk(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    selector = source.index(
        "source /opt/floodman/scripts/select_ai_profile.sh"
    )
    renderer = source.index(
        "/opt/venv/bin/python /opt/floodman/scripts/render_asterisk.py"
    )
    assert selector < renderer
