from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from app.ava.bootstrap import provision_agents


def test_agent_bootstrap_creates_separate_floodman_agents(settings):
    slugs = provision_agents(settings)
    assert "floodman_inbound" in slugs
    assert "floodman_billing" in slugs
    conn = sqlite3.connect(settings.agents_db_path)
    rows = conn.execute("SELECT slug,is_default,tools_json FROM agents ORDER BY slug").fetchall()
    conn.close()
    assert len(rows) >= 7
    default = [row[0] for row in rows if row[1] == 1]
    assert default == ["floodman_inbound"]
    billing = next(row for row in rows if row[0] == "floodman_billing")
    assert "floodman_verify_customer" in billing[2]


def test_asterisk_renderer_uses_persistent_non_root_paths(settings, project_root, tmp_path):
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
    config_dir = tmp_path / "runtime/asterisk/etc"
    asterisk_conf = (config_dir / "asterisk.conf").read_text()
    extensions = (config_dir / "extensions.conf").read_text()
    pjsip = (config_dir / "pjsip.conf").read_text()
    assert str(tmp_path / "runtime/asterisk") in asterisk_conf
    assert f"astmoddir => {tmp_path / 'modules'}" in asterisk_conf
    assert f"astcachedir => {tmp_path / 'runtime/asterisk/cache'}" in asterisk_conf
    assert "runuser" not in asterisk_conf
    assert "AudioSocket(${FLOODMAN_GATE_UUID}" in extensions
    assert "Stasis(asterisk-ai-voice-agent)" in extensions
    assert "[transport-udp]" in pjsip
    assert "type=registration" not in pjsip


def test_asterisk_renderer_supports_ip_authenticated_trunk_without_fake_auth(
    settings, project_root, tmp_path
):
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "runtime-ip"),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "runtime-ip/asterisk/etc"),
            "SIP_TRUNK_MODE": "ip",
            "SIP_SERVER": "sip.example.test",
            "SIP_MATCH": "203.0.113.0/24",
            "SIP_USERNAME": "",
            "SIP_PASSWORD": "",
            "SIP_FROM_USER": "+12315551234",
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
    pjsip = (tmp_path / "runtime-ip/asterisk/etc/pjsip.conf").read_text()
    assert "type=identify" in pjsip
    assert "match=203.0.113.0/24" in pjsip
    assert "identify_by=ip" in pjsip
    assert "outbound_auth=" not in pjsip
    assert "type=auth" not in pjsip


async def test_ami_originate_sets_ava_correlation_variables(settings):
    import asyncio

    from app.outbound.ami import AMIClient

    actions: list[dict[str, list[str]]] = []

    async def read_action(reader: asyncio.StreamReader) -> dict[str, list[str]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        parsed: dict[str, list[str]] = {}
        for line in raw.decode().split("\r\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parsed.setdefault(key.strip(), []).append(value.strip())
        return parsed

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"Asterisk Call Manager/5.0\r\n")
        await writer.drain()

        login = await read_action(reader)
        actions.append(login)
        writer.write(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
        await writer.drain()

        originate = await read_action(reader)
        actions.append(originate)
        action_id = originate["ActionID"][0]
        writer.write(b"Response: Success\r\nMessage: Originate successfully queued\r\n\r\n")
        writer.write(
            (
                "Event: OriginateResponse\r\n"
                f"ActionID: {action_id}\r\n"
                "Response: Success\r\n"
                "Channel: PJSIP/test-00000001\r\n"
                "Reason: 4\r\n\r\n"
            ).encode()
        )
        await writer.drain()

        logoff = await read_action(reader)
        actions.append(logoff)
        writer.write(b"Response: Goodbye\r\nMessage: Thanks\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    settings.ami_host = "127.0.0.1"
    settings.ami_port = port
    settings.ami_secret = "ami-test-secret"
    settings.ami_timeout_seconds = 2.0
    settings.asterisk_trunk = "test-trunk"

    async with server:
        result = await AMIClient(settings).originate(
            phone="+1 (231) 555-0199",
            agent="floodman_callback",
            job_id="job-123",
            purpose="requested_callback",
            customer_id="customer-123",
            extra_variables={"AAVA_CAMPAIGN_ID": "campaign-123"},
        )

    assert result.ok is True
    originate = actions[1]
    variables = set(originate["Variable"])
    assert "AI_AGENT=floodman_callback" in variables
    assert "AI_CONTEXT=floodman_callback" in variables
    assert "AAVA_LEAD_ID=job-123" in variables
    assert "AAVA_CAMPAIGN_ID=campaign-123" in variables
    assert any(value.startswith("AAVA_ATTEMPT_ID=") for value in variables)
    assert "FLOODMAN_JOB_ID=job-123" in variables


def test_asterisk_renderer_supports_registration_authenticated_trunk(
    settings, project_root, tmp_path
):
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path / "runtime-registration"),
            "ASTERISK_CONFIG_DIR": str(tmp_path / "runtime-registration/asterisk/etc"),
            "SIP_TRUNK_MODE": "registration",
            "SIP_SERVER": "sip.example.test",
            "SIP_USERNAME": "floodman-user",
            "SIP_PASSWORD": "floodman-password",
            "SIP_FROM_USER": "+12315551234",
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
    pjsip = (tmp_path / "runtime-registration/asterisk/etc/pjsip.conf").read_text()
    assert "type=auth" in pjsip
    assert "username=floodman-user" in pjsip
    assert "type=registration" in pjsip
    assert "client_uri=sip:floodman-user@sip.example.test" in pjsip
    assert "outbound_auth=floodman-trunk-auth" in pjsip
