from __future__ import annotations

import ast
from pathlib import Path


def test_production_validator_uses_minimal_flux_handshake(
    project_root: Path,
) -> None:
    source = (
        project_root
        / "scripts/validate_production_ai.py"
    ).read_text(encoding="utf-8")
    assert '("model", model)' in source
    assert '("encoding", "linear16")' in source
    assert '("sample_rate", "16000")' in source
    assert '"channels"' not in source
    assert '"language": "en-US"' not in source
    assert '"type": "Configure"' in source
    assert '"ConfigureSuccess"' in source
    assert "request_id=" in source


def test_pinned_ava_flux_patch_uses_turninfo(
    project_root: Path,
) -> None:
    source = (
        project_root
        / "scripts/patch_ava_flux.py"
    ).read_text(encoding="utf-8")
    ast.parse(source)
    assert "Floodman Flux TurnInfo protocol patch" in source
    assert 'msg_type == "TurnInfo"' in source
    assert 'event == "EndOfTurn"' in source
    assert 'message.get("transcript")' in source
    assert '"type": "Configure"' in source
    assert '"type": "CloseStream"' in source
    assert "compression=None" in source
    assert "proxy=None" in source


def test_docker_still_applies_flux_patcher(
    project_root: Path,
) -> None:
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "COPY scripts/patch_ava_flux.py" in dockerfile
    assert (
        "patch_ava_flux.py --ava-root /opt/ava"
        in dockerfile
    )
    assert (
        "Floodman Flux v2 query contract patch"
        in dockerfile
    )

def test_flux_turninfo_logging_does_not_shadow_structlog_event(
    project_root: Path,
) -> None:
    source = (
        project_root
        / "scripts/patch_ava_flux.py"
    ).read_text(encoding="utf-8")
    assert 'turn_event = str(data.get("event") or "")' in source
    assert "event=event" not in source
    assert source.count("turn_event=turn_event") == 2
