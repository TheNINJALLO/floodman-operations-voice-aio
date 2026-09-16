from __future__ import annotations

import struct
from pathlib import Path

import pytest

from app.tel_agent_runtime import DeepgramTurnAssembler, _provider_payload, mulaw_to_slin8


def test_unified_image_pins_tel_agent_without_livekit(project_root: Path) -> None:
    dockerfile = (project_root / "unified/Dockerfile").read_text(encoding="utf-8")
    launcher = (project_root / "unified/start-voice-control.sh").read_text(
        encoding="utf-8"
    )
    adapter = (project_root / "app/tel_agent_runtime.py").read_text(encoding="utf-8")
    phone = (project_root / "app/tel_agent_phone.py").read_text(encoding="utf-8")
    assert "TEL_AGENT_COMMIT=8a5193b2d801714e45ae6981a3506a60d562a99e" in dockerfile
    assert "ENV VOICE_ENGINE=tel_agent" in dockerfile
    assert "PYTHONPATH=/opt/tel-agent:/opt/voice" in launcher
    assert "from agent.session.turn import speak" in dockerfile
    assert "from agent.session.turn import speak" in phone
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


def test_deepgram_segments_are_archived_as_one_complete_turn() -> None:
    assembler = DeepgramTurnAssembler()

    assert assembler.accept({
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {
            "detected_language": "en",
            "alternatives": [{"transcript": "water is in the basement", "confidence": 0.9}],
        },
    }) is None
    turn = assembler.accept({
        "type": "Results",
        "is_final": True,
        "speech_final": True,
        "channel": {
            "detected_language": "en",
            "alternatives": [{"transcript": "near the electrical panel", "confidence": 0.8}],
        },
    })

    assert turn is not None
    assert turn.text == "water is in the basement near the electrical panel"
    assert turn.confidence == pytest.approx(0.85)
    assert turn.language == "en"
    assert assembler.finish() is None


def test_deepgram_utterance_end_flushes_stable_text_only() -> None:
    assembler = DeepgramTurnAssembler()
    assembler.accept({
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {"alternatives": [{"transcript": "stable words"}]},
    })
    assembler.accept({
        "type": "Results",
        "is_final": False,
        "speech_final": False,
        "channel": {"alternatives": [{"transcript": "unstable guess"}]},
    })

    turn = assembler.accept({"type": "UtteranceEnd"})

    assert turn is not None
    assert turn.text == "stable words"
