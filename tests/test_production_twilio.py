from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.outbound.ami import AMIClient, OriginateResult


def _render(project_root: Path, tmp_path: Path, extra: dict[str, str]) -> Path:
    runtime = tmp_path / "runtime"
    modules = tmp_path / "modules"
    modules.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(runtime),
            "ASTERISK_CONFIG_DIR": str(runtime / "asterisk/etc"),
            "ASTERISK_MODULE_DIR": str(modules),
            "SIP_TRUNK_MODE": "twilio",
            "TWILIO_TERMINATION_URI": "floodman-test.pstn.twilio.com",
            "TWILIO_SIP_USERNAME": "FloodmanTest1",
            "TWILIO_SIP_PASSWORD": "LongTestPassword123",
            "TWILIO_FROM_NUMBER": "+12315550100",
            "OUTBOUND_CALLER_ID_NUMBER": "+12315550100",
            "PUBLIC_IP": "203.0.113.10",
            "LOCAL_NET": "172.16.0.0/12",
            "ARI_SECRET": "ari-test-secret-123456789012345",
            "AMI_SECRET": "ami-test-secret-123456789012345",
            # An explicitly blank override must still retain the safe Twilio defaults.
            "TWILIO_SIGNALING_CIDRS": "",
        }
    )
    env.update(extra)
    subprocess.run(
        ["python3", str(project_root / "scripts/render_asterisk.py")],
        check=True,
        cwd=project_root,
        env=env,
    )
    return runtime / "asterisk/etc"


def test_twilio_renderer_builds_non_registering_trunk_and_controlled_test_context(
    settings, project_root, tmp_path
):
    config = _render(project_root, tmp_path, {})
    pjsip = (config / "pjsip.conf").read_text()
    extensions = (config / "extensions.conf").read_text()
    assert "type=registration" not in pjsip
    assert "contact=sip:floodman-test.pstn.twilio.com:5060" in pjsip
    assert "outbound_auth=floodman-trunk-auth" in pjsip
    assert "match=54.172.60.0/30" in pjsip
    assert "match=54.244.51.0/30" in pjsip
    assert "local_net=172.16.0.0/12" in pjsip
    assert "local_net=0.0.0.0/0" not in pjsip
    assert "rtp_symmetric=no" in pjsip
    assert "PJSIP_HEADER(read,Diversion)" in extensions
    assert "[floodman-test-call]" in extensions
    assert "Playback(demo-echotest)" in extensions
    assert "Echo()" in extensions


def test_twilio_secure_renderer_enables_tls_and_sdes_srtp(settings, project_root, tmp_path):
    tls = tmp_path / "tls"
    tls.mkdir()
    cert = tls / "fullchain.pem"
    key = tls / "privkey.pem"
    cert.write_text("test")
    key.write_text("test")
    config = _render(
        project_root,
        tmp_path,
        {
            "TWILIO_SECURE_TRUNKING": "true",
            "SIP_TLS_CERT_FILE": str(cert),
            "SIP_TLS_KEY_FILE": str(key),
        },
    )
    pjsip = (config / "pjsip.conf").read_text()
    assert "[transport-tls]" in pjsip
    assert "protocol=tls" in pjsip
    assert "method=tlsv1_2" in pjsip
    assert f"cert_file={cert}" in pjsip
    assert f"priv_key_file={key}" in pjsip
    assert "media_encryption=sdes" in pjsip
    assert "media_encryption_optimistic=no" in pjsip
    assert "contact=sip:floodman-test.pstn.twilio.com:5061;transport=tls" in pjsip


async def test_ami_controlled_test_call_enters_echo_context(settings):
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
        actions.append(await read_action(reader))
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
                "Channel: PJSIP/test-00000002\r\n"
                "Reason: 4\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        actions.append(await read_action(reader))
        writer.write(b"Response: Goodbye\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    settings.ami_host = "127.0.0.1"
    settings.ami_port = int(server.sockets[0].getsockname()[1])
    settings.ami_secret = "ami-test-secret"
    settings.ami_timeout_seconds = 2.0
    settings.asterisk_trunk = "test-trunk"
    settings.outbound_caller_id_number = "+12315550100"

    async with server:
        result = await AMIClient(settings).originate_test_call(
            phone="+1 (231) 555-0199", label="twilio_audio_test"
        )

    assert result.ok is True
    originate = actions[1]
    assert originate["Context"] == ["floodman-test-call"]
    assert originate["Exten"] == ["s"]
    assert "FLOODMAN_TEST_CALL=1" in originate["Variable"]
    assert "FLOODMAN_TEST_LABEL=twilio_audio_test" in originate["Variable"]


def test_test_call_api_requires_feature_flag_and_allowlist(settings):
    from app.main import create_app

    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(create_app(settings)) as client:
        disabled = client.post(
            "/api/v1/test-calls/outbound",
            headers=headers,
            json={"phone": "+12315550199", "label": "test"},
        )
        assert disabled.status_code == 409

    settings.test_calls_enabled = True
    settings.ami_enabled = True
    settings.test_call_allowlist = ("+12315550199",)
    with TestClient(create_app(settings)) as client:
        async def fake_originate_test_call(*, phone: str, label: str) -> OriginateResult:
            return OriginateResult(
                ok=True,
                action_id="test-action-id",
                message="queued",
                response={},
                answered=True,
                channel="PJSIP/test",
                reason_code="4",
            )

        client.app.state.ami.originate_test_call = fake_originate_test_call
        blocked = client.post(
            "/api/v1/test-calls/outbound",
            headers=headers,
            json={"phone": "+12315550200", "label": "test"},
        )
        assert blocked.status_code == 403
        placed = client.post(
            "/api/v1/test-calls/outbound",
            headers=headers,
            json={"phone": "+12315550199", "label": "test"},
        )
        assert placed.status_code == 200
        assert placed.json()["answered"] is True


def test_web_security_defaults_and_docs_are_closed(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        response = client.get("/livez")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/docs").status_code == 404


def test_twilio_bootstrap_show_config_redacts_all_secrets(project_root, tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "DATA_DIR": str(tmp_path),
            "TWILIO_ACCOUNT_SID": "AC" + "a" * 32,
            "TWILIO_API_KEY": "SK" + "b" * 32,
            "TWILIO_API_KEY_SECRET": "NeverPrintApiSecret123",
            "TWILIO_TRUNK_DOMAIN": "floodman-test.pstn.twilio.com",
            "TWILIO_TERMINATION_URI": "floodman-test.pstn.twilio.com",
            "TWILIO_ORIGINATION_SIP_URI": "sip:203.0.113.10:5060;edge=ashburn",
            "TWILIO_SIP_USERNAME": "FloodmanTest1",
            "TWILIO_SIP_PASSWORD": "NeverPrintSipPassword123",
        }
    )
    completed = subprocess.run(
        ["python3", str(project_root / "scripts/twilio_bootstrap.py"), "show-config"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=project_root,
    )
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert "NeverPrintApiSecret123" not in completed.stdout
    assert "NeverPrintSipPassword123" not in completed.stdout
    assert payload["desired"]["domain_name"] == "floodman-test.pstn.twilio.com"


def test_envfile_loader_is_non_executing_and_preserves_process_secrets(project_root, tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(project_root / "scripts"))
    from envfile import EnvFileError, load_env_files

    marker = tmp_path / "should-not-exist"
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text(
        "\n".join(
            [
                "# comment",
                "FROM_FILE=first",
                'QUOTED="hello world"',
                "HASH=value#literal",
                "TRAILING=value # ignored comment",
                f"NOT_EXECUTED=$(touch {marker})",
            ]
        )
        + "\n"
    )
    second.write_text("FROM_FILE=second\nSECOND_ONLY=loaded\n")
    monkeypatch.setenv("FROM_FILE", "process-wins")
    for key in ("QUOTED", "HASH", "TRAILING", "NOT_EXECUTED", "SECOND_ONLY"):
        monkeypatch.delenv(key, raising=False)

    loaded = load_env_files([first, second])
    assert loaded == 5
    assert os.environ["FROM_FILE"] == "process-wins"
    assert os.environ["QUOTED"] == "hello world"
    assert os.environ["HASH"] == "value#literal"
    assert os.environ["TRAILING"] == "value"
    assert os.environ["NOT_EXECUTED"].startswith("$(touch ")
    assert os.environ["SECOND_ONLY"] == "loaded"
    assert not marker.exists()

    malformed = tmp_path / "malformed.env"
    malformed.write_text("THIS IS NOT AN ASSIGNMENT\n")
    try:
        load_env_files([malformed])
    except EnvFileError as exc:
        assert "expected KEY=VALUE" in str(exc)
    else:
        raise AssertionError("Malformed dotenv input should fail closed")


def test_twilio_bootstrap_loads_split_runtime_and_provisioning_files(project_root, tmp_path):
    runtime = tmp_path / "runtime.env"
    provisioning = tmp_path / "provisioning.env"
    runtime.write_text(
        "\n".join(
            [
                "TWILIO_TERMINATION_URI=floodman-split.pstn.twilio.com",
                "TWILIO_SIP_USERNAME=FloodmanSplit1",
                "TWILIO_SIP_PASSWORD=SplitRuntimePassword123",
                "TWILIO_PHONE_NUMBER=+12315550123",
                f"DATA_DIR={tmp_path}",
            ]
        )
        + "\n"
    )
    provisioning.write_text(
        "\n".join(
            [
                "TWILIO_ACCOUNT_SID=AC" + "a" * 32,
                "TWILIO_API_KEY=SK" + "b" * 32,
                "TWILIO_API_KEY_SECRET=SplitProvisioningSecret123",
                "TWILIO_TRUNK_DOMAIN=floodman-split.pstn.twilio.com",
                "TWILIO_ORIGINATION_SIP_URI=sip:203.0.113.10:5060;edge=ashburn",
            ]
        )
        + "\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TWILIO_") and key != "DATA_DIR"
    }
    completed = subprocess.run(
        [
            "python3",
            str(project_root / "scripts/twilio_bootstrap.py"),
            "--env-file",
            str(runtime),
            "--env-file",
            str(provisioning),
            "show-config",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["desired"]["domain_name"] == "floodman-split.pstn.twilio.com"
    assert payload["desired"]["phone_number"] == "+12315550123"
    assert "SplitProvisioningSecret123" not in completed.stdout
    assert "SplitRuntimePassword123" not in completed.stdout


def test_twilio_phone_number_sid_can_be_resolved_from_e164(project_root):
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(project_root / "scripts"))
    from twilio_bootstrap import BootstrapError, resolve_phone_number

    class FakeAPI:
        def __init__(self, rows):
            self.rows = rows

        def list_incoming_phone_numbers(self, phone_number: str):
            assert phone_number == "+12315550123"
            return self.rows

    desired = SimpleNamespace(phone_number_sid="", phone_number="+12315550123")
    sid = "PN" + "c" * 32
    resolved_sid, row = resolve_phone_number(
        FakeAPI([
            {
                "sid": sid,
                "phone_number": "+12315550123",
                "capabilities": {"voice": True},
            }
        ]),
        desired,
    )
    assert resolved_sid == sid
    assert row["sid"] == sid

    try:
        resolve_phone_number(FakeAPI([]), desired)
    except BootstrapError as exc:
        assert "was not found" in str(exc)
    else:
        raise AssertionError("An unowned Twilio number must not be silently accepted")


def test_twilio_verify_ignores_unreadable_password_rotation(project_root, tmp_path):
    import sys

    sys.path.insert(0, str(project_root / "scripts"))
    from twilio_bootstrap import Desired, build_plan

    trunk_sid = "TK" + "1" * 32
    list_sid = "CL" + "2" * 32
    credential_sid = "CR" + "3" * 32
    phone_sid = "PN" + "4" * 32
    origin_sid = "OU" + "5" * 32
    desired = Desired(
        account_sid="AC" + "0" * 32,
        auth_username="SK" + "a" * 32,
        auth_password="api-secret",
        trunk_sid=trunk_sid,
        trunk_name="Floodman Operations Voice",
        domain_name="floodman-verify.pstn.twilio.com",
        secure=False,
        transfer_mode="disable-all",
        origination_sid=origin_sid,
        origination_name="Floodman Primary Asterisk",
        origination_uri="sip:203.0.113.10:5060;edge=ashburn",
        origination_priority=10,
        origination_weight=10,
        credential_list_sid=list_sid,
        credential_list_name="Floodman Asterisk Credentials",
        credential_sid=credential_sid,
        sip_username="FloodmanVerify1",
        sip_password="VerifyPassword123",
        phone_number_sid=phone_sid,
        phone_number="+12315550123",
        allow_phone_routing_change=False,
        state_path=tmp_path / "twilio.json",
        rotate_password=True,
    )

    class FakeAPI:
        def list_trunks(self):
            return [{
                "sid": trunk_sid,
                "friendly_name": desired.trunk_name,
                "domain_name": desired.domain_name,
                "secure": False,
                "transfer_mode": "disable-all",
                "cnam_lookup_enabled": False,
            }]

        def list_credential_lists(self):
            return [{"sid": list_sid, "friendly_name": desired.credential_list_name}]

        def list_credentials(self, value):
            assert value == list_sid
            return [{"sid": credential_sid, "username": desired.sip_username}]

        def list_trunk_credential_lists(self, value):
            assert value == trunk_sid
            return [{"sid": list_sid}]

        def list_origination_urls(self, value):
            assert value == trunk_sid
            return [{
                "sid": origin_sid,
                "friendly_name": desired.origination_name,
                "sip_url": desired.origination_uri,
                "priority": 10,
                "weight": 10,
                "enabled": True,
            }]

        def fetch_incoming_phone_number(self, value):
            assert value == phone_sid
            return {
                "sid": phone_sid,
                "phone_number": desired.phone_number,
                "capabilities": {"voice": True},
                "trunk_sid": trunk_sid,
                "voice_url": "",
                "voice_application_sid": "",
            }

        def list_phone_numbers(self, value):
            assert value == trunk_sid
            return [{"sid": phone_sid}]

    apply_actions, _ = build_plan(FakeAPI(), desired, include_password_rotation=True)
    verify_actions, _ = build_plan(FakeAPI(), desired, include_password_rotation=False)
    assert next(item for item in apply_actions if item.resource == "credential").action == "update"
    assert next(item for item in verify_actions if item.resource == "credential").action == "keep"


def test_twilio_existing_webhook_routing_requires_explicit_override(project_root, tmp_path):
    import sys

    sys.path.insert(0, str(project_root / "scripts"))
    from twilio_bootstrap import BootstrapError, Desired, build_plan

    trunk_sid = "TK" + "1" * 32
    phone_sid = "PN" + "2" * 32
    desired = Desired(
        account_sid="AC" + "0" * 32,
        auth_username="SK" + "a" * 32,
        auth_password="api-secret",
        trunk_sid=trunk_sid,
        trunk_name="Floodman Operations Voice",
        domain_name="floodman-guard.pstn.twilio.com",
        secure=False,
        transfer_mode="disable-all",
        origination_sid="",
        origination_name="Floodman Primary Asterisk",
        origination_uri="sip:198.51.100.10:5060;edge=ashburn",
        origination_priority=10,
        origination_weight=10,
        credential_list_sid="",
        credential_list_name="Floodman Asterisk Credentials",
        credential_sid="",
        sip_username="FloodmanGuard1",
        sip_password="GuardPassword123",
        phone_number_sid=phone_sid,
        phone_number="+12315550123",
        allow_phone_routing_change=False,
        state_path=tmp_path / "twilio.json",
        rotate_password=False,
    )

    class FakeAPI:
        def fetch_incoming_phone_number(self, value):
            assert value == phone_sid
            return {
                "sid": phone_sid,
                "phone_number": desired.phone_number,
                "capabilities": {"voice": True},
                "trunk_sid": "",
                "voice_url": "https://old.example.com/voice",
                "voice_application_sid": "",
            }

        def list_trunks(self):
            return [{
                "sid": trunk_sid,
                "friendly_name": desired.trunk_name,
                "domain_name": desired.domain_name,
                "secure": False,
                "transfer_mode": "disable-all",
                "cnam_lookup_enabled": False,
            }]

    try:
        build_plan(FakeAPI(), desired)
    except BootstrapError as exc:
        assert "already has Voice routing" in str(exc)
    else:
        raise AssertionError("Existing Twilio webhook routing must not be replaced silently")

    desired.allow_phone_routing_change = True

    class FullFakeAPI(FakeAPI):
        def list_credential_lists(self):
            return []

        def list_origination_urls(self, value):
            return []

        def list_phone_numbers(self, value):
            return []

    actions, ids = build_plan(FullFakeAPI(), desired)
    assert ids["phone_number_sid"] == phone_sid
    assert any(item.resource == "phone_number" and item.action == "attach" for item in actions)


def test_twilio_bootstrap_derives_canonical_domain_from_localized_runtime_uri(
    project_root, tmp_path
):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TWILIO_") and key != "DATA_DIR"
    }
    env.update(
        {
            "DATA_DIR": str(tmp_path),
            "TWILIO_ACCOUNT_SID": "AC" + "a" * 32,
            "TWILIO_API_KEY": "SK" + "b" * 32,
            "TWILIO_API_KEY_SECRET": "ApiSecret123",
            "TWILIO_TERMINATION_URI": "floodman-edge.pstn.ashburn.twilio.com",
            "TWILIO_ORIGINATION_SIP_URI": "sip:198.51.100.10:5060;edge=ashburn",
            "TWILIO_SIP_USERNAME": "FloodmanEdge1",
            "TWILIO_SIP_PASSWORD": "EdgePassword123",
        }
    )
    completed = subprocess.run(
        ["python3", str(project_root / "scripts/twilio_bootstrap.py"), "show-config"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    payload = json.loads(completed.stdout)
    assert payload["desired"]["domain_name"] == "floodman-edge.pstn.twilio.com"


def test_compose_maps_dynamic_sip_and_rtp_ports_to_matching_container_ports(project_root):
    compose = (project_root / "docker-compose.yml").read_text()
    assert '${SIP_PORT:-5060}:${SIP_PORT:-5060}/udp' in compose
    assert '${SIP_TLS_PORT:-5061}:${SIP_TLS_PORT:-5061}/tcp' in compose
    assert (
        '${RTP_START:-10000}-${RTP_END:-10040}:'
        '${RTP_START:-10000}-${RTP_END:-10040}/udp'
    ) in compose


def test_preflight_rejects_non_global_public_ip_and_mismatched_origination_target(
    settings, project_root, monkeypatch
):
    import sys

    sys.path.insert(0, str(project_root / "scripts"))
    from preflight import validate

    settings.sip_trunk_mode = "twilio"
    settings.outbound_caller_id_number = "+12315550123"
    monkeypatch.setenv("SIP_TRUNK_MODE", "twilio")
    monkeypatch.setenv("TWILIO_TERMINATION_URI", "floodman-test.pstn.ashburn.twilio.com")
    monkeypatch.setenv("TWILIO_SIP_USERNAME", "FloodmanTest1")
    monkeypatch.setenv("TWILIO_SIP_PASSWORD", "StrongPassword123")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+12315550123")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12315550123")
    monkeypatch.setenv("PUBLIC_IP", "203.0.113.10")
    monkeypatch.setenv(
        "TWILIO_ORIGINATION_SIP_URI", "sip:198.51.100.10:5060;edge=ashburn"
    )

    checks = {item["name"]: item for item in validate(settings)}
    assert checks["public_ip"]["ok"] is False
    assert checks["twilio_origination_target"]["ok"] is False

    monkeypatch.setenv("PUBLIC_IP", "8.8.8.8")
    monkeypatch.setenv(
        "TWILIO_ORIGINATION_SIP_URI", "sip:8.8.8.8:5060;edge=ashburn"
    )
    checks = {item["name"]: item for item in validate(settings)}
    assert checks["public_ip"]["ok"] is True
    assert checks["twilio_origination_target"]["ok"] is True
    assert checks["twilio_phone_number"]["ok"] is True


def test_preflight_can_require_one_time_twilio_provisioning_fields(
    settings, project_root, monkeypatch
):
    import sys

    sys.path.insert(0, str(project_root / "scripts"))
    from preflight import validate

    settings.sip_trunk_mode = "twilio"
    settings.outbound_caller_id_number = "+12315550123"
    values = {
        "TWILIO_TERMINATION_URI": "floodman-test.pstn.ashburn.twilio.com",
        "TWILIO_SIP_USERNAME": "FloodmanTest1",
        "TWILIO_SIP_PASSWORD": "StrongPassword123",
        "TWILIO_PHONE_NUMBER": "+12315550123",
        "TWILIO_FROM_NUMBER": "+12315550123",
        "PUBLIC_IP": "8.8.8.8",
        "TWILIO_ORIGINATION_SIP_URI": "sip:8.8.8.8:5060;edge=ashburn",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TWILIO_TRUNK_DOMAIN", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY_SECRET", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    checks = {item["name"]: item for item in validate(settings, require_provisioning=True)}
    assert checks["twilio_domain_alignment"]["ok"] is False
    assert checks["twilio_provisioning_credentials"]["ok"] is False

    monkeypatch.setenv("TWILIO_TRUNK_DOMAIN", "floodman-test.pstn.twilio.com")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "a" * 32)
    monkeypatch.setenv("TWILIO_API_KEY", "SK" + "b" * 32)
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "ApiSecret123")
    checks = {item["name"]: item for item in validate(settings, require_provisioning=True)}
    assert checks["twilio_domain_alignment"]["ok"] is True
    assert checks["twilio_provisioning_credentials"]["ok"] is True
    assert checks["twilio_provisioning_region"]["ok"] is True


def test_release_version_metadata_is_consistent(project_root):
    import tomllib

    import app
    from app.main import VERSION as app_main_version

    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    file_version = (project_root / "VERSION").read_text().strip()
    expected = "1.1.1"
    assert app.__version__ == expected
    assert app_main_version == expected
    assert file_version == expected
    assert pyproject["project"]["version"] == expected
