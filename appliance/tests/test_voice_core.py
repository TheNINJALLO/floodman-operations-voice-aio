from pathlib import Path
import pytest
from app.business import BusinessDirectory
from app.config import Settings
from app.db import Database
from app.knowledge import KnowledgeBase
from app.models import VoiceReply
from app.notifications import TeamNotifier
from app.voice_core import VoiceCore

class StubLLM:
    async def extract(self,field,transcript,state): return {}
    async def health(self): return True
    async def answer(self,q,c): return ""

class StubNotifier:
    def __init__(self): self.calls=[]
    async def send(self,call_id,state,kind="lead",partial=False): self.calls.append((kind,partial,state.to_dict())); return 1

def settings(tmp_path,project_root,monkeypatch):
    monkeypatch.setenv("DATA_DIR",str(tmp_path));monkeypatch.setenv("SERVICE_AREA_PATH",str(project_root/"config/service_area.yaml"));monkeypatch.setenv("FLOODMAN_CALLBACK_SLA_HOURS","24")
    return Settings.from_env()

@pytest.mark.asyncio
async def test_complete_intake(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("full-call","+12318840943","+12319354921")
    turns=["Water is coming into my basement","home","this morning","no safety concerns","Josh Aldrich","yes","josh at example dot com","yes","yes","8805 East Melendy Street Ludington Michigan","yes"]
    reply=None
    for turn in turns: reply=await core.process(session,turn)
    assert reply and reply.end_call
    assert session.state.completed
    assert session.state.service_area_status=="published"
    assert notifier.calls[-1][0]=="completed_intake"
    assert "within 24 hours" in reply.text

@pytest.mark.asyncio
async def test_unsupported_and_emergency(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);monkeypatch.setattr(s,"emergency_transfer_number","+12315550001")
    db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    u=core.create_session("unsupported")
    reply=await core.process(u,"I need roof repair")
    assert "not a service" in reply.text.lower() and u.state.service_status=="unsupported"
    e=core.create_session("emergency")
    await core.process(e,"Water is rising by the electrical panel")
    await core.process(e,"home")
    await core.process(e,"right now")
    reply=await core.process(e,"There are sparks and standing water")
    assert reply.transfer_number=="+12315550001"
    assert any(kind=="emergency" for kind,_,_ in notifier.calls)

@pytest.mark.asyncio
async def test_partial_notification_is_idempotent_at_core_level(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("partial")
    await core.process(session,"wet crawl space")
    await core.disconnect(session)
    assert notifier.calls and notifier.calls[-1][1] is True
