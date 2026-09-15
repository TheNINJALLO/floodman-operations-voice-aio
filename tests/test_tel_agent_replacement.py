from __future__ import annotations

import json
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from integrations.tel_agent.service import (
    ControlClient,
    DeepgramUtteranceAccumulator,
    TurnControl,
    _voice_llm_payload,
    load_tool_definitions,
    mulaw_to_slin8,
    reply_with_prompt,
)


def test_tel_agent_is_pinned_without_livekit(project_root: Path) -> None:
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    supervisor = (project_root / "supervisord.conf").read_text(encoding="utf-8")
    service = (project_root / "integrations/tel_agent/service.py").read_text(encoding="utf-8")

    assert "TEL_AGENT_COMMIT=8a5193b2d801714e45ae6981a3506a60d562a99e" in dockerfile
    assert "PYTHONPATH=/opt/tel-agent python -c" in dockerfile
    assert "pip install --no-cache-dir /opt/tel-agent" not in dockerfile
    assert "/opt/tel-agent[voice]" not in dockerfile
    assert "import livekit" not in service.lower()
    assert "agent.session.livekit_room" not in service
    assert "[program:tel-agent]" in supervisor


@pytest.mark.parametrize(
    "relative_path",
    (
        "pterodactyl/egg-floodman-operations-voice-aio.json",
        "egg-floodman-operations-voice-aio-v1.1.1.json",
    ),
)
def test_pterodactyl_eggs_expose_tel_agent_configuration(
    project_root: Path, relative_path: str
) -> None:
    egg = json.loads((project_root / relative_path).read_text(encoding="utf-8"))
    variables = {item["env_variable"]: item for item in egg["variables"]}
    assert variables["VOICE_ENGINE"]["default_value"] == "tel_agent"
    assert variables["TEL_AGENT_ENABLED"]["default_value"] == "1"
    assert variables["AMI_ENABLED"]["default_value"] == "1"
    for name in (
        "DEEPGRAM_API_KEY",
        "GROQ_API_KEY",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
    ):
        assert variables[name]["user_viewable"] is False


def test_mulaw_is_decoded_for_asterisk_slin8() -> None:
    samples = struct.unpack("<hhhh", mulaw_to_slin8(bytes((0xFF, 0x7F, 0x80, 0x00))))
    assert samples == (0, 0, 32124, -32124)


def test_deepgram_segments_become_one_final_customer_utterance() -> None:
    accumulator = DeepgramUtteranceAccumulator("en-US")
    first = accumulator.feed(
        '{"type":"Results","is_final":true,"speech_final":false,'
        '"channel":{"alternatives":[{"transcript":"There is water in",'
        '"confidence":0.9}]}}'
    )
    interim = accumulator.feed(
        '{"type":"Results","is_final":false,"speech_final":false,'
        '"channel":{"alternatives":[{"transcript":"my finished basement"}]}}'
    )
    final = accumulator.feed(
        '{"type":"Results","is_final":true,"speech_final":true,'
        '"channel":{"alternatives":[{"transcript":"my finished basement",'
        '"confidence":0.8}]}}'
    )
    assert first is not None and first.kind == "partial"
    assert interim is not None and interim.kind == "partial"
    assert final is not None and final.kind == "final"
    assert final.text == "There is water in my finished basement"
    assert final.confidence == pytest.approx(0.85)
    assert accumulator.finish() is None


def test_groq_qwen_voice_payload_disables_reasoning(monkeypatch) -> None:
    class ToolSchema:
        def as_schema(self):
            return {"type": "function", "function": {"name": "capture"}}

    monkeypatch.setenv("GROQ_LLM_TEMPERATURE", "0.3")
    monkeypatch.setenv("GROQ_LLM_TOP_P", "0.7")
    monkeypatch.setenv("GROQ_LLM_MAX_TOKENS", "160")
    payload = _voice_llm_payload(
        model="qwen/qwen3.6-27b",
        base_url="https://api.groq.com/openai/v1",
        messages=[{"role": "user", "content": "Hello"}],
        tools=[ToolSchema()],
    )
    assert payload["reasoning_effort"] == "none"
    assert payload["reasoning_format"] == "hidden"
    assert payload["temperature"] == pytest.approx(0.3)
    assert payload["top_p"] == pytest.approx(0.7)
    assert payload["max_tokens"] == 160
    assert payload["tools"][0]["function"]["name"] == "capture"

    compatible_payload = _voice_llm_payload(
        model="qwen/qwen3.6-27b",
        base_url="https://local-llm.invalid/v1",
        messages=[],
        tools=None,
    )
    assert "reasoning_effort" not in compatible_payload
    assert "reasoning_format" not in compatible_payload


def test_all_audiosocket_listeners_default_to_loopback(project_root: Path) -> None:
    example = (project_root / ".env.example").read_text(encoding="utf-8")
    gate = (project_root / "app/call_gate/audio_socket.py").read_text(encoding="utf-8")
    assert "GATE_HOST=127.0.0.1" in example
    assert "TEL_AGENT_HOST=127.0.0.1" in example
    assert "Call-gate AudioSocket must bind to loopback" in gate
    with pytest.raises(ValueError, match="loopback HTTP"):
        ControlClient("https://voice.example.com", "not-a-real-token")


def test_only_enabled_allowlisted_floodman_tools_are_offered(project_root: Path) -> None:
    definitions = load_tool_definitions(project_root / "config/ava/ai-agent.local.yaml")
    assert "floodman_capture_intake_progress" in definitions
    assert definitions["floodman_capture_intake_progress"]["parameters"]["type"] == "object"
    assert "floodman_schedule_inspection" not in definitions
    assert "floodman_reschedule_inspection" not in definitions


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class _TextDelta:
    text: str


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools):
        self.calls += 1
        yield _ToolCall("call-1", "capture", '{"description":"wet basement"}')


class _Tool:
    name = "capture"

    def __init__(self, control: TurnControl) -> None:
        self.control = control

    async def run(self, arguments):
        assert arguments["description"] == "wet basement"
        self.control.direct_message = "What is the full service address?"
        return '{"ok":true}'


@pytest.mark.asyncio
async def test_capture_safe_message_bypasses_model_paraphrase() -> None:
    control = TurnControl()
    provider = _Provider()
    pieces = [
        piece
        async for piece in reply_with_prompt(
            "There is water in my basement",
            system_prompt="Use the intake tool.",
            provider=provider,
            history=[],
            tools=[_Tool(control)],
            control=control,
            text_delta_type=_TextDelta,
        )
    ]
    assert pieces == ["What is the full service address?"]
    assert provider.calls == 1


def test_transcript_turns_are_local_and_idempotent(settings) -> None:
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        payload = {
            "call_id": "tel-call-1",
            "direction": "inbound",
            "sequence": 1,
            "role": "user",
            "text": "There is water in the basement.",
            "confidence": 0.94,
            "language": "en-US",
            "engine": "tel-agent",
        }
        first = client.post("/internal/tel-agent/turn", headers=headers, json=payload)
        second = client.post("/internal/tel-agent/turn", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        events = client.app.state.database.list_call_events(20, "tel-call-1")
        assert len(events) == 1
        assert events[0]["event_type"] == "transcript_turn"
        assert events[0]["payload"]["text"] == payload["text"]


def test_completion_recovers_transcript_from_durable_turns(settings) -> None:
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        for sequence, role, text in (
            (1, "user", "My basement is taking on water."),
            (2, "assistant", "Is the water still entering?"),
        ):
            response = client.post(
                "/internal/tel-agent/turn",
                headers=headers,
                json={
                    "call_id": "tel-recovery-1",
                    "direction": "inbound",
                    "sequence": sequence,
                    "role": role,
                    "text": text,
                },
            )
            assert response.status_code == 200
        completed = client.post(
            "/internal/ava/call-completed",
            headers=headers,
            json={
                "call_id": "tel-recovery-1",
                "caller_number": "+12315550199",
                "direction": "inbound",
                "agent": "floodman_inbound",
                "provider": "tel-agent",
                "outcome": "provider_failure",
                "transcript": None,
                "metadata": {"engine": "tel-agent"},
            },
        )
        assert completed.status_code == 200
        intake = client.app.state.database.get_call_intake("tel-recovery-1")
        assert intake is not None
        assert intake["transcript_text"] == (
            "Caller: My basement is taking on water.\n"
            "Ava: Is the water still entering?"
        )


def test_ava_handoff_prepends_durable_tel_agent_turns(settings) -> None:
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        client.post(
            "/internal/tel-agent/turn",
            headers=headers,
            json={
                "call_id": "tel-handoff-1",
                "direction": "inbound",
                "sequence": 1,
                "role": "user",
                "text": "My basement is flooding.",
            },
        )
        completed = client.post(
            "/internal/ava/call-completed",
            headers=headers,
            json={
                "call_id": "tel-handoff-1",
                "caller_number": "+12315550199",
                "direction": "inbound",
                "agent": "floodman_inbound",
                "provider": "local_hybrid",
                "outcome": "completed",
                "transcript": [
                    {"role": "assistant", "content": "I can continue helping you."}
                ],
                "metadata": {"engine": "ava"},
            },
        )
        assert completed.status_code == 200
        intake = client.app.state.database.get_call_intake("tel-handoff-1")
        assert intake is not None
        assert intake["transcript_text"] == (
            "Caller: My basement is flooding.\n"
            "Ava: I can continue helping you."
        )


def test_transfer_is_allowlisted_and_uses_server_side_channel(settings) -> None:
    class FakeAMI:
        def __init__(self) -> None:
            self.redirected = None

        async def redirect(self, **kwargs):
            self.redirected = kwargs
            return type("Result", (), {"ok": True, "message": "Redirect successful"})()

    settings.ami_enabled = True
    with TestClient(create_app(settings)) as client:
        headers = {"X-Internal-Token": settings.internal_token}
        gate = client.post(
            "/internal/gate/start",
            headers=headers,
            json={
                "call_id": "tel-transfer-1",
                "caller_number": "+12315550199",
                "metadata": {"channel": "PJSIP/floodman-trunk-00000001"},
            },
        ).json()
        fake = FakeAMI()
        client.app.state.ami = fake
        response = client.post(
            "/internal/tel-agent/transfer",
            headers=headers,
            json={"gate_uuid": gate["gate_uuid"], "destination": "live_reception"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert fake.redirected == {
            "channel": "PJSIP/floodman-trunk-00000001",
            "context": "from-internal",
            "extension": "6000",
            "priority": 1,
        }

        rejected = client.post(
            "/internal/tel-agent/transfer",
            headers=headers,
            json={"gate_uuid": gate["gate_uuid"], "destination": "attacker-number"},
        )
        assert rejected.json() == {"ok": False, "error": "transfer_destination_not_configured"}

        fallback = client.post(
            "/internal/tel-agent/fallback",
            headers=headers,
            json={"gate_uuid": gate["gate_uuid"], "reason": "ReadTimeout"},
        )
        assert fallback.status_code == 200
        assert fallback.json()["ok"] is True
        assert fake.redirected == {
            "channel": "PJSIP/floodman-trunk-00000001",
            "context": "floodman-tel-agent-fallback",
            "extension": "s",
            "priority": 1,
        }


def test_tel_agent_is_primary_with_ava_readiness_fallback(
    project_root: Path, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    modules = tmp_path / "modules"
    packaged = tmp_path / "asterisk-share"
    (packaged / "documentation").mkdir(parents=True)
    (packaged / "documentation/core-en_US.xml").write_text("<docs/>", encoding="utf-8")
    modules.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "DATA_DIR": str(runtime),
            "ASTERISK_CONFIG_DIR": str(runtime / "asterisk/etc"),
            "ASTERISK_MODULE_DIR": str(modules),
            "ASTERISK_DATA_DIR": str(packaged),
            "SIP_TRUNK_MODE": "disabled",
            "ARI_SECRET": "ari-test-secret",
            "AMI_SECRET": "ami-test-secret",
            "VOICE_ENGINE": "tel_agent",
        }
    )
    subprocess.run(
        ["python", str(project_root / "scripts/render_asterisk.py")],
        check=True,
        cwd=project_root,
        env=environment,
    )
    extensions = (runtime / "asterisk/etc/extensions.conf").read_text(encoding="utf-8")
    primary = extensions.index("n(tel-agent),TryExec(AudioSocket")
    fallback = extensions.index("n(ava),Set(__AI_AGENT")
    assert primary < fallback
    assert str(runtime / "runtime/tel-agent-ready") in extensions
    assert "GotoIf($[\"${TRYSTATUS}\"=\"FAILED\"]?ava)" in extensions
    assert "n(outbound-tel-agent),TryExec(AudioSocket" in extensions
    assert "n(outbound-ava),NoOp(Floodman outbound AVA fallback)" in extensions
    assert "[floodman-tel-agent-fallback]" in extensions
    assert "Goto(floodman-inbound,s,ava)" in extensions
    assert "Goto(floodman-outbound,s,outbound-ava)" in extensions
