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


def _selector_env(
    tmp_path: Path,
    *,
    requested: str,
    probe_exit: int = 0,
) -> dict[str, str]:
    validator = tmp_path / "fake_validator.py"
    validator.write_text(
        "import os, pathlib, sys\n"
        "exit_code = int(os.environ.get('FAKE_PROBE_EXIT', '0'))\n"
        "if exit_code == 0 and '--marker' in sys.argv:\n"
        "    marker = pathlib.Path(sys.argv[sys.argv.index('--marker') + 1])\n"
        "    marker.parent.mkdir(parents=True, exist_ok=True)\n"
        "    marker.write_text('validated\\n', encoding='utf-8')\n"
        "sys.exit(exit_code)\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(CLOUD_KEYS)
    env.update(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "FLOODMAN_AI_PROFILE": requested,
            "FLOODMAN_PRODUCTION_STRICT": "false",
            "FLOODMAN_CLOUD_AUDIO_PROBE": "false",
            "FLOODMAN_VALIDATOR_PYTHON": sys.executable,
            "FLOODMAN_PRODUCTION_VALIDATOR": str(validator),
            "FAKE_PROBE_EXIT": str(probe_exit),
        }
    )
    return env


def _read_status(tmp_path: Path) -> dict[str, str]:
    path = (
        tmp_path
        / "data"
        / "runtime"
        / "ai-profile-status.env"
    )
    assert path.is_file()
    output: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        output[key] = value
    return output


def test_production_selector_writes_compact_status(
    project_root: Path,
    tmp_path: Path,
) -> None:
    script = project_root / "scripts/select_ai_profile.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=project_root,
        env=_selector_env(
            tmp_path,
            requested="production_hybrid",
            probe_exit=0,
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status = _read_status(tmp_path)
    assert status["STATE"] == "selected"
    assert status["REQUESTED_PROFILE"] == "production_hybrid"
    assert status["SELECTED_PROFILE"] == "production_hybrid"
    assert status["AVA_PIPELINE"] == "floodman_production"
    assert status["LOCAL_AI_ENABLED"] == "false"
    assert status["PRODUCTION_VALIDATION"] == "passed"
    assert "Floodman AI STATUS:" in result.stdout


def test_blocked_production_writes_reason_before_exit(
    project_root: Path,
    tmp_path: Path,
) -> None:
    script = project_root / "scripts/select_ai_profile.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=project_root,
        env=_selector_env(
            tmp_path,
            requested="production_hybrid",
            probe_exit=1,
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    status = _read_status(tmp_path)
    assert status["STATE"] == "blocked"
    assert status["REQUESTED_PROFILE"] == "production_hybrid"
    assert status["PRODUCTION_VALIDATION"] == "not-passed"
    assert "provider checks failed" in status["REASON"]
    assert "will not fall back to local_hybrid" in result.stderr


def test_local_selector_status_is_unambiguous(
    project_root: Path,
    tmp_path: Path,
) -> None:
    script = project_root / "scripts/select_ai_profile.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=project_root,
        env=_selector_env(
            tmp_path,
            requested="local_hybrid",
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status = _read_status(tmp_path)
    assert status["SELECTED_PROFILE"] == "local_hybrid"
    assert status["AVA_PIPELINE"] == "local_hybrid"
    assert status["LOCAL_AI_ENABLED"] == "true"


def test_production_profile_cannot_start_local_models(
    project_root: Path,
) -> None:
    script = project_root / "scripts/run_local_ai.sh"
    env = os.environ.copy()
    env.update(
        {
            "FLOODMAN_AI_PROFILE": "production_hybrid",
            "ENABLE_LOCAL_AI_SERVER": "true",
        }
    )
    process = subprocess.Popen(
        ["bash", str(script)],
        cwd=project_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        assert (
            line
            == "Floodman local AI disabled because "
            "production_hybrid is selected"
        )
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _fake_asterisk(tmp_path: Path) -> tuple[Path, Path]:
    fake = tmp_path / "asterisk"
    args_file = tmp_path / "args.txt"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$ASTERISK_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, args_file


def test_asterisk_runner_is_quiet_by_default(
    project_root: Path,
    tmp_path: Path,
) -> None:
    script = project_root / "scripts/run_asterisk.sh"
    fake, args_file = _fake_asterisk(tmp_path)

    env = os.environ.copy()
    env.update(
        {
            "ASTERISK_MODE": "embedded",
            "ASTERISK_BIN": str(fake),
            "ASTERISK_ARGS_FILE": str(args_file),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "etc"),
        }
    )
    result = subprocess.run(
        ["bash", str(script)],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args == [
        "-f",
        "-C",
        str(tmp_path / "etc" / "asterisk.conf"),
    ]


def test_asterisk_verbose_mode_remains_opt_in(
    project_root: Path,
    tmp_path: Path,
) -> None:
    script = project_root / "scripts/run_asterisk.sh"
    fake, args_file = _fake_asterisk(tmp_path)

    env = os.environ.copy()
    env.update(
        {
            "ASTERISK_MODE": "embedded",
            "ASTERISK_BIN": str(fake),
            "ASTERISK_ARGS_FILE": str(args_file),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "etc"),
            "ASTERISK_STARTUP_VERBOSITY": "3",
        }
    )
    result = subprocess.run(
        ["bash", str(script)],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args[-1] == "-vvv"
