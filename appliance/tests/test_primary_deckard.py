from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from app.briefings import DECKARD_MODEL, PrimaryBriefingClient, PostCallBriefings
from app.config import Settings
from app.db import Database
from app.llm import LocalLLM
from app.model_profile import select_primary_model
from app.tel_agent_runtime import TelAgentLLM
from test_cpu_runtime import shell, shell_path


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PRIMARY_MODEL_PROFILE", raising=False)
    monkeypatch.delenv("DECKARD_MODEL_PATH", raising=False)
    monkeypatch.delenv("BRIEFING_BACKEND", raising=False)
    # Old panel variables must not retain the generic model accidentally.
    monkeypatch.setenv("LLAMA_MODEL_REPO", "Qwen/Qwen3-4B-GGUF")
    monkeypatch.setenv("LLAMA_MODEL_FILENAME", "Qwen3-4B-Q4_K_M.gguf")
    monkeypatch.setenv("LLAMA_MODEL_ALIAS", "floodman-qwen3-4b")
    monkeypatch.setenv("LLAMA_MODEL_PATH", str(tmp_path / "old-qwen.gguf"))
    return Settings.from_env()


def test_live_and_background_defaults_select_deckard_despite_old_panel_settings(configured):
    model = select_primary_model(configured.model_dir)
    assert configured.primary_model_profile == "deckard"
    assert configured.llama_model_alias == DECKARD_MODEL == model.alias
    assert configured.voice_engine == "tel_agent"
    assert configured.llm_model_path.name == "Qwen3.6-40B-Deck-Opus-NEO-CODE-HERE-2T-OT-Q4_K_M.gguf"
    assert model.revision == "a07c606ff200709e7094b1469aa8a1d9028720c9"
    assert model.sha256 == "6533e19802f02af3524ae499a5f10b07667913c8ffa6bf4f055e83ea525a9fba"
    assert model.size_bytes == 24253604896
    assert configured.briefing_backend == "primary" and configured.briefing_model == DECKARD_MODEL
    service = PostCallBriefings(configured, Database(configured.database_path), lambda: 0)
    assert isinstance(service.client, PrimaryBriefingClient)
    assert service.client.model == configured.llama_model_alias


def test_custom_model_requires_explicit_selection(configured, monkeypatch):
    monkeypatch.setenv("PRIMARY_MODEL_PROFILE", "custom")
    selected = Settings.from_env()
    assert selected.llama_model_alias == "floodman-qwen3-4b"
    assert selected.llm_model_path.name == "old-qwen.gguf"
    monkeypatch.delenv("LLAMA_MODEL_ALIAS")
    with pytest.raises(ValueError, match="require"):
        Settings.from_env()


@pytest.mark.parametrize("field,value", [("PRIMARY_MODEL_PROFILE", "typo"), ("BRIEFING_BACKEND", "typo")])
def test_unknown_profiles_are_rejected(configured, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError, match=field):
        Settings.from_env()


def download_module(project_root):
    spec = importlib.util.spec_from_file_location("primary_download", project_root / "scripts/download_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def small_model(configured):
    data = b"GGUFverified-test-model"
    return replace(select_primary_model(configured.model_dir), size_bytes=len(data),
                   sha256=hashlib.sha256(data).hexdigest()), data


def test_first_boot_fetches_the_pinned_revision_and_verifies_weights(configured, project_root):
    module = download_module(project_root)
    model, data = small_model(configured)
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        target = kwargs["local_dir"] / kwargs["filename"]
        target.write_bytes(data)
        return str(target)

    module.ensure_primary_model(model, configured.data_dir, fetch=fetch)
    assert model.path.read_bytes() == data
    assert requests[0]["repo_id"] == model.repository
    assert requests[0]["revision"] == model.revision
    assert requests[0]["filename"] == model.filename
    module.ensure_primary_model(model, configured.data_dir, fetch=lambda **_kwargs: pytest.fail("Valid resident model must be reused"))


@pytest.mark.parametrize("corrupt", [b"GGUF", b"GGUFcorrupt-test-model!", b"XXXXverified-test-model"])
def test_existing_corrupt_or_wrong_weights_cannot_be_loaded(configured, project_root, corrupt):
    module = download_module(project_root)
    model, _ = small_model(configured)
    model.path.parent.mkdir(parents=True)
    model.path.write_bytes(corrupt)
    with pytest.raises(ValueError):
        module.ensure_primary_model(model, configured.data_dir, fetch=lambda **_kwargs: pytest.fail("No silent substitution"))


def test_new_downloads_stay_under_data_dir_but_verified_read_only_mounts_are_reusable(configured, project_root, tmp_path):
    module = download_module(project_root)
    model, data = small_model(configured)
    outside = replace(model, path=tmp_path.parent / (tmp_path.name + "-external") / model.filename)
    with pytest.raises(ValueError, match="DATA_DIR"):
        module.ensure_primary_model(outside, configured.data_dir, fetch=lambda **_kwargs: pytest.fail("No download outside DATA_DIR"))
    outside.path.parent.mkdir()
    outside.path.write_bytes(data)
    module.ensure_primary_model(outside, configured.data_dir, fetch=lambda **_kwargs: pytest.fail("Reuse existing immutable weights"))


def test_native_launcher_uses_the_same_model_and_safe_quoted_path(configured, project_root, tmp_path, monkeypatch):
    runtime = tmp_path / "native server"
    runtime.mkdir()
    executable = runtime / "llama-server"
    executable.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8", newline="\n")
    executable.chmod(0o755)
    monkeypatch.setenv("LLAMA_RUNTIME_DIR", shell_path(runtime))
    monkeypatch.setenv("PYTHON_BIN", shell_path(Path(sys.executable)))
    monkeypatch.setenv("LOCAL_AI_DEVICE", "cpu")
    monkeypatch.setenv("DECKARD_MODEL_PATH", str(tmp_path / "model's $(echo injected).gguf"))
    selected = Settings.from_env()
    result = subprocess.run([shell(), str(project_root / "scripts/llama-server.sh")], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[args.index("--alias") + 1] == selected.llama_model_alias == DECKARD_MODEL
    assert args[args.index("--model") + 1] == str(selected.llm_model_path)
    assert "$(echo injected)" in args[args.index("--model") + 1]
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--n-gpu-layers") + 1] == "0"
    monkeypatch.setenv("PRIMARY_MODEL_PROFILE", "invalid")
    invalid = subprocess.run([shell(), str(project_root / "scripts/llama-server.sh")], capture_output=True, text=True, timeout=5)
    assert invalid.returncode != 0 and invalid.stdout == ""


def fake_stream_provider(monkeypatch, seen, *, fail=False):
    for name in ("agent", "agent.providers", "agent.providers.llm"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    config = ModuleType("agent.config")
    config.LlmSettings = lambda **kwargs: SimpleNamespace(**kwargs)
    base = ModuleType("agent.providers.llm.base")

    class TextDelta:
        def __init__(self, text):
            self.text = text

    base.TextDelta = TextDelta
    provider = ModuleType("agent.providers.llm.openai_compatible")

    class OpenAICompatibleLLM:
        def __init__(self, settings):
            self._settings = settings

        async def _read(self, client, url, payload, headers):
            seen.append((url, payload, headers))
            if fail:
                raise RuntimeError("stream unavailable")
            yield TextDelta("Hello from Deckard.")

    provider.OpenAICompatibleLLM = OpenAICompatibleLLM
    for module in (config, base, provider):
        monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stream", [False, True])
async def test_streamed_voice_and_completion_recovery_keep_deckard(configured, monkeypatch, fail_stream):
    monkeypatch.setenv("LOCAL_AI_ONLY", "false")
    monkeypatch.setenv("GROQ_API_KEY", "old-retained-cloud-key")
    settings = Settings.from_env()
    seen = []
    fake_stream_provider(monkeypatch, seen, fail=fail_stream)
    original = httpx.AsyncClient
    completions = []

    def response(request):
        completions.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Resident Deckard recovery."}}]})

    def factory(*args, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(*args, transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    llm = TelAgentLLM(settings)
    assert llm._cloud_key == ""
    result = "".join([piece async for piece in llm.stream_text([{"role": "user", "content": "Hello"}])])
    assert result == ("Resident Deckard recovery." if fail_stream else "Hello from Deckard.")
    assert seen[0][1]["model"] == DECKARD_MODEL
    assert seen[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
    if fail_stream:
        assert completions[0]["model"] == DECKARD_MODEL


@pytest.mark.asyncio
async def test_primary_briefing_and_extraction_use_resident_deckard(configured, monkeypatch):
    original = httpx.AsyncClient
    requests = []
    source = {"saved_intake": {}, "messages": [{"message_index": 1, "role": "caller", "text": "Wet basement."}]}
    draft = {"summary": "Caller reported a wet basement.", "evidence": [{"message_index": 1, "quote": "Wet basement."}]}

    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        content = json.dumps(draft if "response_format" in payload else {"value": "Josh"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original(*args, transport=httpx.MockTransport(response), **kwargs))
    assert await LocalLLM(configured).extract("name", "Josh", {}) == {"value": "Josh"}
    assert await PrimaryBriefingClient(configured).generate(source) == draft
    assert all(payload["model"] == DECKARD_MODEL for payload in requests)
    assert requests[-1]["response_format"]["type"] == "json_schema"
    assert requests[-1]["response_format"]["schema"]["additionalProperties"] is False
    assert all(payload["chat_template_kwargs"] == {"enable_thinking": False} for payload in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised,healthy", [(DECKARD_MODEL, True), ("floodman-qwen3-4b", False)])
async def test_readiness_checks_the_served_model_identity(configured, monkeypatch, advertised, healthy):
    original = httpx.AsyncClient
    requests = []

    def response(request):
        requests.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": advertised}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original(*args, transport=httpx.MockTransport(response), **kwargs))
    assert await LocalLLM(configured).health() is healthy
    assert requests == ["/health", "/v1/models"]
