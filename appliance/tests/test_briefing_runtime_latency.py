import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.briefings import PostCallBriefings
from app.config import Settings
from app.db import Database


@pytest.mark.asyncio
async def test_background_queue_storage_does_not_hold_the_voice_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POST_CALL_BRIEFINGS_ENABLED", "true")
    settings = Settings.from_env()
    database = Database(settings.database_path)
    gate = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_claim():
        loop.call_soon_threadsafe(entered.set)
        gate.wait(timeout=2)
        return None

    monkeypatch.setattr(database, "claim_call_briefing", slow_claim)
    service = PostCallBriefings(settings, database, lambda: 0, client=SimpleNamespace())
    work = asyncio.create_task(service.process_one())
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        await asyncio.sleep(0.01)
        assert not work.done(), "Queue storage must run outside the audio event loop"
    finally:
        gate.set()
        assert await asyncio.wait_for(work, timeout=1) is False
