from __future__ import annotations

from pathlib import Path
import json


def test_runtime_binds_to_port_8002(
    project_root: Path,
) -> None:
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")
    supervisor = (
        project_root / "supervisor/supervisord.conf"
    ).read_text(encoding="utf-8")
    config = (
        project_root / "app/config.py"
    ).read_text(encoding="utf-8")

    assert "WEB_PORT=8002" in dockerfile
    assert "EXPOSE 8002/tcp" in dockerfile
    assert (
        "http://127.0.0.1:${WEB_PORT}/ready"
        in dockerfile
    )
    assert "--port %(ENV_WEB_PORT)s" in supervisor
    assert (
        "http://127.0.0.1:%(ENV_WEB_PORT)s/ready"
        in supervisor
    )
    assert '_int("WEB_PORT", 8002)' in config

    for text in (dockerfile, supervisor, config):
        assert "8003" not in text


def test_egg_declares_web_port_8002(
    project_root: Path,
) -> None:
    egg = json.loads(
        (
            project_root
            / "pterodactyl/"
            "egg-floodman-voice-appliance.json"
        ).read_text(encoding="utf-8")
    )
    variables = {
        item["env_variable"]: item
        for item in egg["variables"]
    }

    assert variables["WEB_PORT"]["default_value"] == "8002"
    assert (
        variables["PUBLIC_BASE_URL"]["default_value"]
        == "http://127.0.0.1:8002"
    )
