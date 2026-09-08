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

class HallucinatingLLM(StubLLM):
    async def extract(self,field,transcript,state):
        if field == "name": return {"value":"Baldrige"}
        if field == "email": return {"value":"aldrich@example.com"}
        return {}

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
    reply=None;replies=[]
    for turn in turns:
        reply=await core.process(session,turn);replies.append(reply.text)
    assert reply and reply.end_call
    assert session.state.completed
    assert session.state.service_area_status=="published"
    assert notifier.calls[-1][0]=="completed_intake"
    assert "within 24 hours" in reply.text
    assert not any(text.startswith(("Got it.", "Understood.", "Thanks.", "Great.", "Perfect.")) for text in replies)

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


@pytest.mark.asyncio
async def test_spelled_email_confirmation_does_not_say_dash(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("spelled-email")
    session.state.stage="email"
    reply=await core.process(session,"J-O-A-C-H, S-H at gmail.com.")
    assert session.state.email=="joachsh@gmail.com"
    assert "dash" not in reply.text.lower()


def test_greeting_uses_concise_floodman_introduction():
    assert VoiceCore.greeting() == "Hello. This is Alex with Floodman. How may I help you today?"


@pytest.mark.asyncio
async def test_volunteered_home_context_skips_redundant_question(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("natural-home")

    reply=await core.process(session,"I have mold in my house")

    assert session.state.property_context=="Residential property"
    assert session.state.stage=="timing_summary"
    assert "home or a business" not in reply.text.lower()
    assert not reply.text.startswith("Got it.")
    assert "mold or musty conditions" in reply.text


@pytest.mark.asyncio
async def test_home_misheard_as_hope_advances_without_repeating_question(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("home-hope");session.state.stage="property_context"

    reply=await core.process(session,"hope")

    assert session.state.property_context=="Residential property"
    assert session.state.stage=="timing_summary"
    assert "home or a business" not in reply.text.lower()


@pytest.mark.asyncio
async def test_contact_fields_cannot_be_rewritten_or_invented_by_llm(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),HallucinatingLLM(),notifier)
    session=core.create_session("grounded-contact");session.state.stage="name"

    name_reply=await core.process(session,"My name is Josh Aldrich")
    assert session.state.name=="Josh Aldrich"
    assert "Baldrige" not in name_reply.text

    session.state.stage="email"
    email_reply=await core.process(session,"dot com")
    assert session.state.email==""
    assert session.state.stage=="email"
    assert "aldrich@example.com" not in email_reply.text
    assert "only heard part" in email_reply.text.lower()


@pytest.mark.asyncio
async def test_no_input_waits_before_safe_callback_fallback(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("patient-no-input")

    first=await core.no_input(session)
    second=await core.no_input(session)
    third=await core.no_input(session)

    assert "wait a moment" in first.text.lower() and not first.end_call
    assert "say hello" in second.text.lower() and not second.end_call
    assert third.end_call
    assert notifier.calls[-1][0]=="partial_no_input"
