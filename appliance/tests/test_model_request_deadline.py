import asyncio
from types import SimpleNamespace

import pytest

from app.tel_agent_runtime import TelAgentLLM


@pytest.mark.asyncio
async def test_complete_voice_model_request_has_a_total_deadline(monkeypatch):
    settings = SimpleNamespace(local_ai_only=True, primary_model_profile="deckard", llama_base_url="http://127.0.0.1:8081/v1", llama_model_alias="floodman-deckard:40b-q4_k_m", llama_timeout_seconds=0.03)
    llm = TelAgentLLM(settings)
    closed = asyncio.Event()

    async def delayed_stream(*_args, **_kwargs):
        try:
            yield "partial"
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(llm, "stream_text", delayed_stream)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(llm._chat([{"role": "user", "content": "A deployment test"}]), timeout=1)
    assert closed.is_set()
