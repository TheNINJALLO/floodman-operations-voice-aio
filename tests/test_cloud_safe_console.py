from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_provider_validator_covers_all_cloud_services(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/validate_production_ai.py"
    ).read_text(encoding="utf-8")
    assert "api.deepgram.com/v2/listen" in source
    assert "api.groq.com/openai/v1/chat/completions" in source
    assert "api.elevenlabs.io/v1" in source
    assert "ulaw_8000" in source


def test_profile_selector_falls_back_safely(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/select_ai_profile.sh"
    ).read_text(encoding="utf-8")
    assert "validate_production_ai.py" in source
    assert "switching to local_hybrid" in source
    assert "ELEVENLABS_VOICE_ID" in source


def test_live_console_hides_notice_but_full_log_keeps_it(
    project_root: Path,
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "runtime"),
            "ASTERISK_CONFIG_DIR": str(
                tmp_path / "runtime/asterisk/etc"
            ),
            "ASTERISK_MODULE_DIR": str(tmp_path / "modules"),
            "SIP_TRUNK_MODE": "disabled",
            "ARI_SECRET": "ari-test-secret",
            "AMI_SECRET": "ami-test-secret",
        }
    )
    subprocess.run(
        [
            "python",
            str(project_root / "scripts/render_asterisk.py"),
        ],
        check=True,
        env=env,
        cwd=project_root,
    )
    logger_conf = (
        tmp_path / "runtime/asterisk/etc/logger.conf"
    ).read_text(encoding="utf-8")
    assert "console => warning,error,verbose" in logger_conf
    assert "console => notice" not in logger_conf
    assert (
        "full => notice,warning,error,verbose,debug"
        in logger_conf
    )


def test_ava_requires_cloud_validation_marker(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/run_ava.sh"
    ).read_text(encoding="utf-8")
    assert "production-ai-validated.json" in source
