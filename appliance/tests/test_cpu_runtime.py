from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.tts import LocalTTS
from app.tel_agent_runtime import TelAgentLLM, TelAgentSTT, TelAgentTTS


def shell() -> str:
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
        pytest.skip("Git Bash is required for executable shell checks on Windows")
    candidate = shutil.which("bash")
    if not candidate:
        pytest.skip("bash is required for executable shell checks")
    return candidate


def shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


@pytest.mark.parametrize("device", [None, "cpu", "cuda"])
def test_device_and_listening_settings(tmp_path, monkeypatch, device):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LOCAL_AI_DEVICE", raising=False)
    monkeypatch.delenv("NO_INPUT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("INPUT_PREROLL_MS", raising=False)
    if device:
        monkeypatch.setenv("LOCAL_AI_DEVICE", device)

    settings = Settings.from_env()

    assert settings.local_ai_device == (device or "cpu")
    assert settings.no_input_timeout_seconds == 12.0
    assert settings.input_preroll_ms == 240
    assert settings.faster_whisper_threads <= 4
    assert settings.kokoro_threads == 2


@pytest.mark.parametrize("value,expected", [("-1", 2.0), ("90", 60.0), ("invalid", 12.0)])
def test_no_input_timeout_is_bounded(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NO_INPUT_TIMEOUT_SECONDS", value)
    assert Settings.from_env().no_input_timeout_seconds == expected


def test_invalid_device_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_AI_DEVICE", "typo")
    with pytest.raises(ValueError, match="LOCAL_AI_DEVICE"):
        Settings.from_env()


def test_invalid_local_only_flag_cannot_enable_cloud_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_AI_ONLY", "typo")
    with pytest.raises(ValueError, match="LOCAL_AI_ONLY"):
        Settings.from_env()


@pytest.mark.parametrize("local_only", [None, "true", "false"])
def test_cloud_credentials_require_explicit_opt_in(tmp_path, monkeypatch, local_only):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LOCAL_AI_ONLY", raising=False)
    monkeypatch.setenv("PRIMARY_MODEL_PROFILE", "custom")
    monkeypatch.setenv("LLAMA_MODEL_REPO", "test/local-model")
    monkeypatch.setenv("LLAMA_MODEL_FILENAME", "test.gguf")
    monkeypatch.setenv("LLAMA_MODEL_ALIAS", "test-local")
    if local_only is not None:
        monkeypatch.setenv("LOCAL_AI_ONLY", local_only)
    for name in ("GROQ_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        monkeypatch.setenv(name, "test-retained-setting")
    settings = Settings.from_env()
    providers = [TelAgentLLM(settings), TelAgentSTT(settings), TelAgentTTS(settings)]

    assert settings.local_ai_only is (local_only != "false")
    assert all(bool(provider._cloud_key) is (local_only == "false") for provider in providers)
    if settings.local_ai_only:
        assert all("local" in provider.provider_name for provider in providers)
        assert providers[1].streaming is False
        assert providers[0]._base_url == settings.llama_base_url


@pytest.mark.parametrize("device", [None, "cpu", "typo"])
def test_hardware_gate_without_nvidia(project_root, monkeypatch, device):
    monkeypatch.delenv("LOCAL_AI_DEVICE", raising=False)
    if device:
        monkeypatch.setenv("LOCAL_AI_DEVICE", device)
    # No external utilities can be resolved by the script in this test.
    env = dict(os.environ, PATH="")
    result = subprocess.run(
        [shell(), str(project_root / "scripts/gpu-preflight.sh")],
        env=env, capture_output=True, text=True, timeout=5,
    )
    if device == "typo":
        assert result.returncode != 0
        assert "LOCAL_AI_DEVICE" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "CPU" in result.stdout


@pytest.mark.parametrize("device,expected_layers,expected_flash", [
    ("cpu", "0", "off"), ("cuda", "99", "on"),
])
def test_launcher_selects_the_requested_backend(project_root, tmp_path, monkeypatch, device, expected_layers, expected_flash):
    runtime = tmp_path / "runtime with spaces"
    runtime.mkdir()
    executable = runtime / "llama-server"
    executable.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8", newline="\n")
    executable.chmod(0o755)
    monkeypatch.setenv("LOCAL_AI_DEVICE", device)
    monkeypatch.setenv("LLAMA_GPU_LAYERS", "99")  # A retained GPU egg setting.
    monkeypatch.delenv("LLAMA_FLASH_ATTN", raising=False)
    monkeypatch.setenv("LLAMA_RUNTIME_DIR", shell_path(runtime))
    monkeypatch.setenv("PYTHON_BIN", shell_path(Path(sys.executable)))
    monkeypatch.setenv("LLAMA_API_KEY", "test-key")

    result = subprocess.run(
        [shell(), str(project_root / "scripts/llama-server.sh")],
        capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 0, result.stderr
    arguments = result.stdout.splitlines()
    assert arguments[arguments.index("--n-gpu-layers") + 1] == expected_layers
    assert arguments[arguments.index("--flash-attn") + 1] == expected_flash
    assert arguments[arguments.index("--host") + 1] == "127.0.0.1"
    assert arguments[arguments.index("--parallel") + 1] == "1"


@pytest.mark.parametrize("device,expected_code", [("cpu", 0), ("cuda", 1)])
def test_preflight_requires_gpu_only_for_cuda(project_root, tmp_path, monkeypatch, capsys, device, expected_code):
    spec = importlib.util.spec_from_file_location("cpu_preflight", project_root / "scripts/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_AI_DEVICE", device)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("INTERNAL_TOKEN", "b" * 32)
    monkeypatch.setenv("SIP_MODE", "disabled")
    settings = Settings.from_env()
    for path in (settings.llm_model_path, settings.kokoro_model_path, settings.kokoro_voices_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(sys, "argv", ["preflight.py"])

    assert module.main() == expected_code
    output = capsys.readouterr().out
    assert ("gpu_memory" in output) is (device == "cuda")
    assert ("[FAIL]" in output) is (device == "cuda")


@pytest.mark.asyncio
async def test_kokoro_uses_a_bounded_cpu_pool_without_spinning(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KOKORO_THREADS", "3")
    settings = Settings.from_env()
    sessions = []
    loaded = object()

    class Options:
        def __init__(self):
            self.entries = {}

        def add_session_config_entry(self, name, value):
            self.entries[name] = value

    def create_session(path, *, sess_options, providers):
        sessions.append((path, sess_options, providers))
        return "bounded-session"

    class Kokoro:
        @staticmethod
        def from_session(session, voices):
            assert session == "bounded-session"
            assert voices == str(settings.kokoro_voices_path)
            return loaded

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(SessionOptions=Options, InferenceSession=create_session))
    monkeypatch.setitem(sys.modules, "kokoro_onnx", SimpleNamespace(Kokoro=Kokoro))
    tts = LocalTTS(settings)

    await tts.start()
    await tts.start()

    assert tts._kokoro is loaded
    assert len(sessions) == 1
    path, options, providers = sessions[0]
    assert path == str(settings.kokoro_model_path)
    assert providers == ["CPUExecutionProvider"]
    assert options.intra_op_num_threads == 3
    assert options.inter_op_num_threads == 1
    assert options.entries == {
        "session.intra_op.allow_spinning": "0",
        "session.inter_op.allow_spinning": "0",
    }
