import asyncio

import pytest

from app.models import VoiceReply
from test_tel_agent_phone import agent, session


@pytest.mark.asyncio
async def test_slow_wording_cancels_partial_rewrite_and_keeps_required_question(monkeypatch):
    monkeypatch.setenv("TEL_AGENT_WORDING_BUDGET_MS", "200")
    closed = asyncio.Event()

    class SlowModel:
        async def stream_spoken_reply(self, **_kwargs):
            try:
                yield "Absolutely. "
                await asyncio.Event().wait()
            finally:
                closed.set()

    phone = agent()
    phone.llm = SlowModel()
    reply = VoiceReply(text="Which room is affected?")

    async def collect():
        return [part async for part in phone._reply_stream(session(), "Water downstairs", reply, "issue")]

    assert await asyncio.wait_for(collect(), timeout=1) == [reply.text]
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_wording_failure_preserves_the_whole_required_reply(partial):
    class BrokenModel:
        async def stream_spoken_reply(self, **_kwargs):
            if partial:
                yield "Of course. "
            raise ConnectionError("synthetic model failure")

    phone = agent()
    phone.llm = BrokenModel()
    reply = VoiceReply(text="Which room is affected?")
    assert [part async for part in phone._reply_stream(session(), "Water", reply, "issue")] == [reply.text]


@pytest.mark.asyncio
async def test_incomplete_wording_cannot_skip_the_next_question():
    reply = VoiceReply(text="Which room is affected?")
    assert [part async for part in agent(("Absolutely.",))._reply_stream(session(), "Water", reply, "issue")] == [reply.text]


@pytest.mark.asyncio
async def test_call_cancellation_propagates_through_optional_wording():
    started, closed = asyncio.Event(), asyncio.Event()

    class WaitingModel:
        async def stream_spoken_reply(self, **_kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()

    phone = agent()
    phone.llm = WaitingModel()

    async def collect():
        return [part async for part in phone._reply_stream(session(), "Water", VoiceReply(text="Which room?"), "issue")]

    task = asyncio.create_task(collect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
