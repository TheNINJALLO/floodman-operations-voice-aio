from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_local_ai_launcher_accepts_pterodactyl_booleans(
    project_root: Path,
) -> None:
    script = (
        project_root / "scripts/run_local_ai.sh"
    ).read_text(encoding="utf-8")
    assert "1|true|yes|on" in script
    assert '"${ENABLE_LOCAL_AI_SERVER:-true}" != "true"' not in script
    assert "Starting Floodman local AI" in script


def test_ava_publishes_real_stasis_readiness(
    project_root: Path,
) -> None:
    script = (
        project_root / "scripts/run_ava.sh"
    ).read_text(encoding="utf-8")
    assert "ava-stasis-ready" in script
    assert "/applications/${AVA_APP}" in script
    assert "Floodman AVA Stasis application ready" in script
    assert 'rm -f "${AVA_READY_FILE}"' in script
    assert 'wait "${AVA_PID}"' in script


def test_inbound_call_waits_instead_of_immediate_hangup(
    project_root: Path,
    tmp_path: Path,
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
    extensions = (
        tmp_path / "runtime/asterisk/etc/extensions.conf"
    ).read_text(encoding="utf-8")

    marker = str(tmp_path / "runtime/runtime/ava-stasis-ready")
    assert f"STAT(e,{marker})" in extensions
    assert "TryExec(Playback(one-moment-please))" in extensions
    assert "AVA_STASIS_ATTEMPTS" in extensions
    assert "Goto(ava-readiness)" in extensions
    assert "ava-stasis-failed" in extensions
    assert "outbound-provider-failure" in extensions
