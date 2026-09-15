from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app.tel_agent_runtime import _provider_payload, mulaw_to_slin8


def test_unified_image_pins_tel_agent_without_livekit(project_root: Path) -> None:
    dockerfile = (project_root / "unified/Dockerfile").read_text(encoding="utf-8")
    launcher = (project_root / "unified/start-voice-control.sh").read_text(
        encoding="utf-8"
    )
    adapter = (project_root / "app/tel_agent_runtime.py").read_text(encoding="utf-8")
    assert "TEL_AGENT_COMMIT=8a5193b2d801714e45ae6981a3506a60d562a99e" in dockerfile
    assert "ENV VOICE_ENGINE=tel_agent" in dockerfile
    assert "PYTHONPATH=/opt/tel-agent:/opt/voice" in launcher
    assert "import livekit" not in adapter.lower()
    assert "[voice]" not in dockerfile


def test_tel_agent_mulaw_output_converts_to_asterisk_slin8() -> None:
    samples = struct.unpack("<hhhh", mulaw_to_slin8(bytes((0xFF, 0x7F, 0x80, 0x00))))
    assert samples == (0, 0, 32124, -32124)


def test_provider_payload_preserves_phone_reasoning_controls() -> None:
    payload = _provider_payload(
        model="qwen/qwen3.6-27b",
        base_url="https://api.groq.com/openai/v1",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=160,
        temperature=0.3,
    )
    assert payload["reasoning_effort"] == "none"
    assert payload["reasoning_format"] == "hidden"
    assert payload["max_tokens"] == 160

    local = _provider_payload(
        model="floodman-qwen3-4b",
        base_url="http://127.0.0.1:8081/v1",
        messages=[],
        max_tokens=100,
        temperature=0,
    )
    assert local["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_format" not in local


def test_invalid_voice_engine_is_rejected(monkeypatch, tmp_path: Path) -> None:
    from app.config import Settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config"))
    monkeypatch.setenv("VOICE_ENGINE", "not-a-real-engine")
    with pytest.raises(ValueError, match="VOICE_ENGINE"):
        Settings.from_env()
