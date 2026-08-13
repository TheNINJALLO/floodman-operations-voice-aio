from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_ava_waits_for_local_ai_before_starting(project_root: Path) -> None:
    script = (project_root / "scripts/run_ava.sh").read_text(encoding="utf-8")
    assert "LOCAL_AI_READY_TIMEOUT_SECONDS" in script
    assert "socket.create_connection" in script
    assert "Floodman local AI ready" in script
    assert "AVA_PID" in script
    assert "ava-stasis-ready" in script
    assert "/applications/${AVA_APP}" in script
    assert script.index("socket.create_connection") < script.index(
        '/opt/venv/bin/python "${AVA_RUNTIME_DIR}/main.py" &'
    )
    assert script.index(
        '/opt/venv/bin/python "${AVA_RUNTIME_DIR}/main.py" &'
    ) < script.index("/applications/${AVA_APP}")


def test_direct_gate_has_wall_clock_no_audio_fail_open(
    project_root: Path,
) -> None:
    source = (
        project_root / "app/call_gate/audio_socket.py"
    ).read_text(encoding="utf-8")
    assert 'decision.metadata["no_audio_fail_open"] = True' in source
    assert "minimum_audio_seconds = max(0.35" in source
    assert "elapsed >= silence_timeout and not enough_audio" in source


def test_twilio_renderer_uses_ip_only_endpoint_identification(
    project_root: Path,
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "runtime"),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "runtime/asterisk/etc"),
            "ASTERISK_MODULE_DIR": str(tmp_path / "modules"),
            "SIP_TRUNK_MODE": "twilio",
            "PUBLIC_IP": "203.0.113.10",
            "LOCAL_NET": "172.16.0.0/12",
            "TWILIO_TERMINATION_URI": "floodman-test.pstn.ashburn.twilio.com",
            "TWILIO_SIP_USERNAME": "FloodmanTestUser",
            "TWILIO_SIP_PASSWORD": "Floodman-Test-2026",
            "TWILIO_FROM_NUMBER": "+12315550100",
            "OUTBOUND_CALLER_ID_NUMBER": "+12315550100",
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
    pjsip = (
        tmp_path / "runtime/asterisk/etc/pjsip.conf"
    ).read_text(encoding="utf-8")
    assert "[global]" in pjsip
    assert "endpoint_identifier_order=ip" in pjsip
    assert "user_agent=Floodman-Voice-Gateway" in pjsip
    assert "unidentified_request_count=2" in pjsip
    assert "match=54.172.60.0/30" in pjsip
    assert "anonymous" not in pjsip
