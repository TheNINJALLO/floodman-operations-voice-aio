from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from app.outbound.ami import AMIClient


class _Writer:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, value: bytes) -> None:
        self.payload += value

    async def drain(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ami_action_response_skips_unrelated_event() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"Event: FullyBooted\r\nPrivilege: system,all\r\n\r\n"
        b"Response: Success\r\nActionID: wanted\r\n"
        b"Message: Originate successfully queued\r\n\r\n"
    )
    writer = _Writer()
    result = await AMIClient._send_action(
        writer,  # type: ignore[arg-type]
        reader,
        [
            ("Action", "Originate"),
            ("ActionID", "wanted"),
            ("Channel", "PJSIP/+12315550100@floodman-trunk"),
        ],
        1.0,
    )
    assert result["Response"] == "Success"
    assert result["ActionID"] == "wanted"
    assert b"ActionID: wanted" in writer.payload


def test_live_call_runtime_contract(project_root: Path) -> None:
    entrypoint = (
        project_root / "scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    renderer = (
        project_root / "scripts/render_asterisk.py"
    ).read_text(encoding="utf-8")
    overlay = yaml.safe_load(
        (
            project_root / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )

    assert "normalize_local_model_tier()" in entrypoint
    assert "LOCAL_STT_MODEL_PATH" in entrypoint
    assert "LOCAL_LLM_MODEL_PATH" in entrypoint
    assert "LOCAL_TTS_MODEL_PATH" in entrypoint
    assert "2026-08-13.1" in entrypoint
    assert 'INBOUND_TEST_MODE", "off"' in renderer
    assert "{inbound_test_lines.rstrip()}" in renderer
    assert "Gosub(from-internal" in renderer
    assert overlay["pipelines"]["local_hybrid"]["llm"] == "local_llm"
    assert overlay["providers"]["local_llm"]["enabled"] is True
    assert overlay["on_provider_failure"] == "dialplan_redirect"
    assert "Floodman" in overlay["llm"]["initial_greeting"]
