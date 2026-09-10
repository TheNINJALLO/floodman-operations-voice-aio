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

class RecordingLLM(StubLLM):
    def __init__(self): self.fields=[]
    async def extract(self,field,transcript,state): self.fields.append(field);return {}

class AnsweringLLM(StubLLM):
    def __init__(self): self.questions=[]
    async def answer(self,q,c): self.questions.append((q,c)); return "Floodman can inspect the condition and explain the appropriate next step."

class StubNotifier:
    def __init__(self): self.calls=[]
    async def send(self,call_id,state,kind="lead",partial=False): self.calls.append((kind,partial,state.to_dict())); return 1

class FailingNotifier(StubNotifier):
    async def call_started(self,call_id,state): raise RuntimeError("notification unavailable")
    async def send(self,call_id,state,kind="lead",partial=False): raise RuntimeError("notification unavailable")

def settings(tmp_path,project_root,monkeypatch):
    monkeypatch.setenv("DATA_DIR",str(tmp_path));monkeypatch.setenv("SERVICE_AREA_PATH",str(project_root/"config/service_area.yaml"));monkeypatch.setenv("FLOODMAN_CALLBACK_SLA_HOURS","24")
    return Settings.from_env()

@pytest.mark.asyncio
async def test_complete_intake(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("full-call","+12318840943","+12319354921")
    turns=["Water is coming into my basement","home","the basement floor and drywall","this morning and it is still active","a supply line broke and it reached two rooms","no safety concerns","Josh Aldrich","yes","josh at example dot com","yes","yes","8805 East Melendy Street Ludington Michigan 49431","yes"]
    reply=None;replies=[]
    for turn in turns:
        reply=await core.process(session,turn);replies.append(reply.text)
    assert reply and reply.end_call
    assert session.state.completed
    assert session.state.service_area_status=="published"
    assert notifier.calls[-1][0]=="completed_intake"
    assert session.state.affected_area=="the basement floor and drywall"
    assert session.state.source_summary=="a supply line broke and it reached two rooms"
    assert "within 24 hours" in reply.text
    assert reply.text.endswith("Goodbye.")
    assert not any(text.startswith(("Got it.", "Understood.", "Thanks.", "Great.", "Perfect.")) for text in replies)

@pytest.mark.asyncio
async def test_unsupported_and_emergency(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);monkeypatch.setattr(s,"emergency_transfer_number","+12315550001")
    db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    u=core.create_session("unsupported")
    reply=await core.process(u,"I need roof repair")
    assert "not a service" in reply.text.lower() and u.state.service_status=="unsupported"
    e=core.create_session("emergency")
    reply=await core.process(e,"Water is rising by the electrical panel")
    assert reply.transfer_number=="+12315550001"
    assert e.state.urgency=="emergency" and e.state.department=="emergency"
    assert any(kind=="emergency" for kind,_,_ in notifier.calls)

    safe=core.create_session("not-emergency")
    reply=await core.process(safe,"A pipe broke, but the water is off and there are no electrical concerns")
    assert not reply.transfer_number
    assert safe.state.urgency=="normal"

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
    assert "j, o, a, c, h, s, h, at, g, m, a, i, l, dot, c, o, m" in reply.text.lower()
    assert reply.speech_parts[-1]=="Is that correct?"
    assert reply.pause_between_parts_ms==300
    assert reply.speech_part_speeds==(s.email_readback_speed,None)


@pytest.mark.asyncio
async def test_split_email_fragments_are_recombined_without_saying_skip(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("split-email");session.state.stage="email"

    partial=await core.process(session,"J.O.")
    assert session.state.stage=="email" and session.state.email==""
    assert session.state.metadata["email_fragments"]==["J.O."]
    assert "skip" not in partial.text.lower()

    reply=await core.process(session,"aldrich at gmail dot com")
    assert session.state.email=="joaldrich@gmail.com"
    assert "email_fragments" not in session.state.metadata
    assert reply.speech_part_speeds==(s.email_readback_speed,None)
    assert "skip" not in reply.text.lower()


@pytest.mark.asyncio
async def test_complete_email_restart_replaces_stale_domain_fragment(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("email-restart");session.state.stage="email"

    await core.process(session,"at gmail.com")
    reply=await core.process(session,"josh at floodband.com")

    assert session.state.email=="josh@floodband.com"
    assert session.state.stage=="confirm_email"
    assert "email_fragments" not in session.state.metadata
    assert "j, o, s, h, at, f, l, o, o, d, b, a, n, d, dot, c, o, m" in reply.text.lower()


@pytest.mark.asyncio
async def test_email_capture_cannot_loop_forever(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("email-limit");session.state.stage="email"

    await core.process(session,"unclear")
    await core.process(session,"still unclear")
    reply=await core.process(session,"not understood")

    assert session.state.email==""
    assert session.state.email_status=="unavailable"
    assert session.state.confirmations["email"]=="unavailable"
    assert session.state.stage=="phone"
    assert session.state.metadata["unconfirmed_email_fragments"]==["unclear","still unclear","not understood"]
    assert "confirm it by phone" in reply.text.lower()
    assert "callback number" in reply.text.lower()


@pytest.mark.asyncio
async def test_email_correction_during_confirmation_is_used_immediately(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("email-correction");session.state.stage="confirm_email";session.state.email="wrong@example.com";session.state.email_status="provided"

    reply=await core.process(session,"No, it's josh at floodband dot com")

    assert session.state.email=="josh@floodband.com"
    assert session.state.stage=="confirm_email"
    assert "j, o, s, h, at, f, l, o, o, d, b, a, n, d, dot, c, o, m" in reply.text.lower()


@pytest.mark.asyncio
async def test_email_confirmation_cannot_loop_forever(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("email-confirm-limit");session.state.stage="confirm_email";session.state.email="josh@example.com";session.state.email_status="provided"

    await core.process(session,"I did not hear the question")
    reply=await core.process(session,"What was that")

    assert session.state.email==""
    assert session.state.email_status=="unavailable"
    assert session.state.stage=="phone"
    assert session.state.metadata["unconfirmed_email"]=="josh@example.com"
    assert "verify it by phone" in reply.text.lower()


def test_greeting_uses_concise_floodman_introduction():
    assert VoiceCore.greeting() == "Hello. This is Alex with Floodman. How may I help you today?"


@pytest.mark.asyncio
async def test_volunteered_home_context_skips_redundant_question(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("natural-home")

    reply=await core.process(session,"I have mold in my house")

    assert session.state.property_context=="Residential property"
    assert session.state.stage=="affected_area"
    assert "home or a business" not in reply.text.lower()
    assert not reply.text.startswith("Got it.")
    assert "where on the property" in reply.text.lower()


@pytest.mark.asyncio
async def test_home_misheard_as_hope_advances_without_repeating_question(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    session=core.create_session("home-hope");session.state.stage="property_context"

    reply=await core.process(session,"hope")

    assert session.state.property_context=="Residential property"
    assert session.state.stage=="affected_area"
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
    assert "only caught part" in email_reply.text.lower()
    assert "skip" not in email_reply.text.lower()


@pytest.mark.asyncio
async def test_verbatim_intake_fields_do_not_wait_for_llm_extraction(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();llm=RecordingLLM();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),llm,notifier)
    session=core.create_session("fast-verbatim")

    session.state.stage="timing_summary"
    await core.process(session,"A couple of weeks ago")
    session.state.stage="affected_area"
    await core.process(session,"The basement floor and south wall")
    session.state.stage="source_summary"
    await core.process(session,"A wall leak spread under the flooring")
    session.state.stage="safety_summary"
    await core.process(session,"No safety concerns")
    session.state.stage="address"
    await core.process(session,"8805 East Melendy Street Ludington Michigan")

    assert llm.fields==[]
    assert session.state.timing_summary=="A couple of weeks ago"
    assert session.state.affected_area=="The basement floor and south wall"
    assert session.state.source_summary=="A wall leak spread under the flooring"
    assert session.state.safety_summary=="No safety concerns"
    assert session.state.address=="8805 East Melendy Street Ludington Michigan"


def test_normal_call_prompts_are_prepared_for_zero_generation_delay(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);notifier=StubNotifier();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),notifier)
    phrases=core.warm_phrases()

    assert VoiceCore.greeting() in phrases
    assert "Is this a home or a business?" in phrases
    assert "When did you first notice the mold or musty conditions?" in phrases
    assert "Where on the property is the problem, and which rooms or materials are affected?" in phrases
    assert "What do you think caused it, and how far has it spread?" in phrases
    assert "Any electrical, sewage, or other safety concerns?" in phrases
    assert "I can get these details to the right team. What name should I put this under?" in phrases
    assert "What's the best email for you?" in phrases
    assert not any("skip" in phrase.lower() for phrase in phrases)
    assert len(phrases)==len(set(phrases))


@pytest.mark.asyncio
async def test_unheard_interrupted_question_is_reasked_and_prior_answer_is_preserved(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("interrupted-question")
    session.state.stage="safety_summary"
    session.state.source_summary="A drain overflowed in the back room."
    core.note_prompt_interrupted(session,previous_stage="source_summary",playback_fraction=0.04)

    reply=await core.process(session,"and part of the way into the hallway")

    assert session.state.stage=="safety_summary"
    assert session.state.safety_summary==""
    assert session.state.source_summary.endswith("and part of the way into the hallway")
    assert reply.text=="Sorry, I cut you off. Any electrical, sewage, or other safety concerns?"


@pytest.mark.asyncio
async def test_answer_to_interrupted_question_is_accepted_without_repeating(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("answered-interruption");session.state.stage="safety_summary"
    core.note_prompt_interrupted(session,previous_stage="source_summary",playback_fraction=0.20)

    reply=await core.process(session,"No safety concerns")

    assert session.state.safety_summary=="No safety concerns"
    assert session.state.stage=="name"
    assert "What name" in reply.text


@pytest.mark.asyncio
async def test_approved_questions_are_answered_then_active_intake_question_is_reasked(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);llm=AnsweringLLM();core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),llm,StubNotifier())
    session=core.create_session("free-flow-question");session.state.stage="property_context"

    reply=await core.process(session,"Should I touch wet electrical equipment?")

    assert llm.questions and "wet electrical equipment" in llm.questions[0][1].lower()
    assert reply.text.startswith("Floodman can inspect")
    assert reply.text.endswith("Is this a home or a business?")
    assert session.state.stage=="property_context"


@pytest.mark.asyncio
async def test_free_estimate_is_answered_while_intake_moves_forward(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("free-estimate")

    reply=await core.process(session,"Can I talk to somebody about a free estimate?")

    assert "free inspections and consultations" in reply.text
    assert reply.text.endswith("Is this a home or a business?")
    assert session.state.stage=="property_context"


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


@pytest.mark.asyncio
async def test_name_and_address_confirmations_use_a_real_brief_pause_and_digit_readback(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),StubNotifier())
    session=core.create_session("confirmation-pause");session.state.stage="name"

    name_reply=await core.process(session,"My name is Josh Aldrich")

    assert name_reply.text=="I heard Josh Aldrich. Is that correct?"
    assert name_reply.speech_parts==("I heard Josh Aldrich","Is that correct?")
    assert name_reply.pause_between_parts_ms==300

    session.state.stage="address"
    address_reply=await core.process(session,"8805 Main Street Detroit Michigan 48207")
    assert "eight, eight, zero, five, Main Street" in address_reply.text
    assert "four, eight, two, zero, seven" in address_reply.text
    assert not any(character.isdigit() for character in address_reply.text)


@pytest.mark.asyncio
async def test_notification_failure_does_not_trigger_technical_call_failure(tmp_path,project_root,monkeypatch):
    s=settings(tmp_path,project_root,monkeypatch);db=Database(s.database_path);core=VoiceCore(s,db,BusinessDirectory(s.service_area_path),KnowledgeBase(project_root/"knowledge"),StubLLM(),FailingNotifier())
    session=core.create_session("fail-open-notification");session.state.stage="confirm_address";session.state.address="1 Main Street";session.state.confirmations["address"]="pending"

    await core.call_started(session)
    reply=await core.process(session,"yes")

    assert reply.end_call
    assert session.state.completed
    assert "all set" in reply.text.lower()
    assert db.get_call(session.call_id)["notification_status"]=="delivery_error"
