import configparser
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def render(project_root, tmp_path, monkeypatch):
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "app_audiosocket.so").touch()
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASTERISK_MODULE_DIR", str(modules))
    monkeypatch.setenv("SIP_MODE", "twilio")
    monkeypatch.setenv("SIP_SERVER", "floodman-test.pstn.ashburn.twilio.com")
    monkeypatch.setenv("SIP_USERNAME", "testuser")
    monkeypatch.setenv("SIP_PASSWORD", "test-secret-not-live")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+12315550100")
    for name in ("SIP_MATCH_ADDRESSES", "SIP_FROM_DOMAIN", "SIP_FROM_USER", "SIP_TRANSPORT", "SIP_TLS_CERT_FILE", "SIP_TLS_KEY_FILE", "OUTBOUND_CALLER_ID_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location("twilio_renderer", project_root / "scripts/render_asterisk.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run():
        assert module.main() == 0
        result = configparser.ConfigParser(interpolation=None, strict=False)
        result.read(tmp_path / "data/asterisk/etc/pjsip.conf")
        return result

    return run


def test_twilio_receives_by_source_ip_and_authenticates_only_outbound(render):
    config = render()
    endpoint = config["floodman-trunk"]
    assert "auth" not in endpoint
    assert endpoint["outbound_auth"] == "floodman-trunk-auth"
    assert endpoint["identify_by"] == "ip"
    assert endpoint["from_domain"] == "floodman-test.pstn.twilio.com"
    assert endpoint["from_user"] == "+12315550100"
    assert endpoint["rtp_symmetric"] == "no"
    assert config["global"]["endpoint_identifier_order"] == "ip"
    assert "floodman-registration" not in config
    assert config["floodman-trunk-aor"]["contact"] == "sip:floodman-test.pstn.ashburn.twilio.com:5060"


def test_generic_carrier_keeps_registration_and_inbound_auth(render, monkeypatch):
    monkeypatch.setenv("SIP_MODE", "generic")
    config = render()
    assert config["floodman-trunk"]["auth"] == "floodman-trunk-auth"
    assert config["floodman-registration"]["outbound_auth"] == "floodman-trunk-auth"


def test_disabled_carrier_opens_no_sip_listener_even_with_stale_credentials(render, monkeypatch):
    monkeypatch.setenv("SIP_MODE", "disabled")
    assert render().sections() == ["global"]


def test_tls_requires_certificate_and_key(render, monkeypatch):
    monkeypatch.setenv("SIP_TRANSPORT", "tls")
    with pytest.raises(ValueError, match="TLS requires"):
        render()


def test_tls_enables_certificate_verification_and_required_srtp(render, monkeypatch, tmp_path):
    cert, key = tmp_path / "test.crt", tmp_path / "test.key"
    cert.touch()
    key.touch()
    monkeypatch.setenv("SIP_TLS_CERT_FILE", str(cert))
    monkeypatch.setenv("SIP_TLS_KEY_FILE", str(key))
    monkeypatch.setenv("SIP_TRANSPORT", "tls")
    monkeypatch.setenv("SIP_SERVER", "sips:floodman-test.pstn.twilio.com:5061")
    config = render()
    assert config["transport-tls"]["verify_server"] == "yes"
    assert config["transport-tls"]["cert_file"] == str(cert)
    assert config["floodman-trunk"]["media_encryption"] == "sdes"
    assert config["floodman-trunk"]["media_encryption_optimistic"] == "no"
    assert "floodman-registration" not in config


def test_supervisor_supports_project_specific_persistent_directory(project_root):
    source = (project_root / "supervisor/supervisord.conf").read_text()
    assert "/home/container/data" not in source
    assert "%(ENV_DATA_DIR)s/asterisk/etc/asterisk.conf" in source
    assert "%(ENV_DATA_DIR)s/logs/llama.log" in source
