from __future__ import annotations

import pytest

from app.business import BusinessDirectory
from app.config import Settings
from app.conversation import capture_volunteered, contact_fields, memory, request_kind
from app.db import Database
from app.knowledge import KnowledgeBase
from app.intake import detect_emergency
from app.models import IntakeState
from app.voice_core import VoiceCore
from app.tel_agent_phone import TelAgentPhoneAgent


class OfflineModel:
    async def extract(self, *args):
        raise AssertionError("Conversation planning must not wait for model extraction")

    async def answer(self, *args):
        return ""


class Notifier:
    def __init__(self, database):
        self.database, self.sent = database, []

    async def send(self, call_id, state, kind="lead", partial=False):
        # Notifications see the same current intake already saved locally.
        assert self.database.get_call(call_id)["snapshot"] == state.to_dict()
        self.sent.append((kind, partial))
        return 1


@pytest.fixture
def core(tmp_path, monkeypatch, project_root):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    database = Database(settings.database_path)
    return VoiceCore(settings, database, BusinessDirectory(project_root / "config/service_area.yaml"),
                     KnowledgeBase(project_root / "knowledge"), OfflineModel(), Notifier(database))


@pytest.mark.asyncio
async def test_opening_details_skip_property_room_timing_and_source_questions(core):
    session = core.create_session("rich-opening")
    reply = await core.process(session, "My name is Sam Jones. The basement at my house gets damp every time it rains. No safety concerns.")
    assert session.state.name == "Sam Jones"
    assert session.state.property_context == "Residential property"
    assert "basement" in session.state.affected_area
    assert session.state.timing_summary and session.state.source_summary and session.state.safety_summary
    assert session.state.stage == "water_condition"
    assert reply.text == "Are you seeing standing water, or mainly dampness?"
    reply = await core.process(session, "Mainly damp walls, no standing water.")
    assert session.state.stage == "confirm_name"
    assert "home or" not in reply.text and "which room" not in reply.text


@pytest.mark.asyncio
async def test_multi_field_answer_skips_settled_followups(core):
    session = core.create_session("multi-field")
    await core.process(session, "I need help with mold in my house")
    assert session.state.stage == "affected_area"
    await core.process(session, "It's on the bathroom wall, started yesterday after a pipe leak. No safety concerns.")
    assert session.state.stage == "name"
    assert session.state.timing_summary and session.state.source_summary and session.state.safety_summary


@pytest.mark.asyncio
async def test_unknown_optional_details_do_not_loop(core):
    session = core.create_session("unknowns")
    await core.process(session, "There is mold in my basement")
    assert session.state.stage == "timing_summary"
    await core.process(session, "I don't know")
    assert session.state.stage == "safety_summary"
    await core.process(session, "No safety concerns")
    assert session.state.stage == "name"


@pytest.mark.parametrize("text,expected", [
    ("Please send me a copy of the warranty", "warranty"),
    ("I am calling about an existing quote, quote number 00123", "quote_followup"),
    ("Please leave a message for Jim about a vendor invoice", "message"),
    ("I'm calling about a general liability audit", "message"),
    ("I want to check my job application", "message"),
    ("I have a question about my bill", "message"),
    ("You came to our property and we haven't heard back", "message"),
    ("I need to reschedule my appointment", "message"),
    ("I have mold in my house", "service"),
])
@pytest.mark.asyncio
async def test_request_kinds_and_short_message_paths(core, text, expected):
    assert request_kind(text) == expected
    session = core.create_session("kind-" + expected)
    reply = await core.process(session, text)
    assert memory(session.state)["kind"] == expected
    if expected != "service":
        assert session.state.stage == "name"
        assert "home or" not in reply.text and "room" not in reply.text
        assert "address" not in contact_fields(session.state)


@pytest.mark.asyncio
async def test_general_message_finishes_without_property_or_email_interview(core):
    session = core.create_session("message", "+12315550100")
    replies = []
    for text in ("Leave a message for Jim about the vendor audit", "Sam Jones", "yes", "yes", "nothing else"):
        replies.append(await core.process(session, text))
    assert session.state.completed and replies[-1].end_call
    assert not any("service address" in reply.text or "email" in reply.text or "home or" in reply.text for reply in replies)
    assert core.notifier.sent == [("completed_intake", False)]


@pytest.mark.asyncio
async def test_caller_id_confirmation_uses_a_short_natural_question(core):
    session = core.create_session("caller-id", "+12315550100")
    for text in ("Leave a message for Jim about the vendor audit", "Sam Jones", "yes"):
        reply = await core.process(session, text)
    assert session.state.stage == "confirm_phone"
    assert reply.text == "Is this the best number to call you back on?"
    assert reply.text in core.warm_phrases()
    await core.process(session, "No, 2315550199")
    assert session.state.phone == "+12315550199"
    assert "one, nine, nine" in core.database.get_call(session.call_id)["messages"][-1]["text"]


@pytest.mark.asyncio
async def test_warranty_email_is_relevant_but_property_address_is_not(core):
    session = core.create_session("warranty", "+12315550100")
    for text in ("I need the warranty documentation", "Sam Jones", "yes", "yes"):
        await core.process(session, text)
    assert session.state.stage == "email"
    await core.process(session, "I prefer not to give an email")
    assert session.state.stage == "anything_else"
    reply = await core.process(session, "No thanks")
    assert reply.end_call and session.state.completed and not session.state.address


@pytest.mark.asyncio
async def test_volunteered_contacts_are_reused_and_still_confirmed(core):
    session = core.create_session("contacts", "+12315550000")
    await core.process(session, "My name is Sam Jones. Leave a message for Jim. Call me at 2315550199. My email is sam at example dot com.")
    assert session.state.stage == "confirm_name"
    assert session.state.phone == "+12315550199" and session.state.email == "sam@example.com"
    await core.process(session, "yes")
    assert session.state.stage == "confirm_phone"
    await core.process(session, "yes")
    assert session.state.stage == "confirm_email"


@pytest.mark.asyncio
async def test_caller_can_refuse_name_and_still_leave_message(core):
    session = core.create_session("refusal", "+12315550100")
    await core.process(session, "Please leave a message about the quote number 123")
    await core.process(session, "I don't want to give my name")
    assert session.state.stage == "confirm_phone"
    await core.process(session, "yes")
    reply = await core.process(session, "That is it")
    assert reply.end_call and not session.state.completed
    assert core.notifier.sent == [("completed_intake", True)]


@pytest.mark.asyncio
async def test_question_answered_without_consuming_current_field(core):
    session = core.create_session("question")
    await core.process(session, "I have mold on a basement wall")
    stage = session.state.stage
    before = session.state.timing_summary
    reply = await core.process(session, "Is the estimate free?")
    assert "free inspections and consultations" in reply.text
    assert session.state.stage == stage and session.state.timing_summary == before


@pytest.mark.parametrize("field,old,utterance,new", [
    ("name", "Wrong Name", "No, it's Sam Jones", "Sam Jones"),
    ("phone", "+12315550000", "No, 2315550199", "+12315550199"),
    ("address", "1 Wrong Road", "No, it's 20 Main Street, Ludington Michigan 49431", "20 Main Street, Ludington Michigan 49431"),
])
@pytest.mark.asyncio
async def test_same_turn_contact_correction_is_used(core, field, old, utterance, new):
    session = core.create_session("correction-" + field)
    session.state.stage = "confirm_" + field
    setattr(session.state, field, old)
    await core.process(session, utterance)
    assert getattr(session.state, field) == new and session.state.stage == "confirm_" + field
    assert session.state.confirmations.get(field) != new


@pytest.mark.asyncio
async def test_asked_for_pause_does_not_consume_field_or_generate_speech_during_grace(core):
    session = core.create_session("pause")
    session.state.stage = "email"
    reply = await core.process(session, "Give me a minute")
    assert reply.text == "Take your time." and session.state.stage == "email"
    for _ in range(2):
        reply = await core.no_input(session)
        assert reply.wait_for_caller and not reply.text and not reply.end_call
    assert session.no_input_count == 0
    await core.process(session, "sam at example dot com")
    assert session.state.email == "sam@example.com" and session.state.stage == "confirm_email"


@pytest.mark.asyncio
async def test_goodbye_is_final_and_does_not_reprompt_or_transfer(core):
    session = core.create_session("goodbye")
    session.state.stage = "done"
    for _ in range(4):
        reply = await core.no_input(session)
        assert reply.end_call and not reply.transfer_number and "still there" not in reply.text
    assert not core.notifier.sent


@pytest.mark.asyncio
async def test_urgent_and_human_routes_still_preempt_intake(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    monkeypatch.setattr(core.settings, "live_transfer_number", "+12315550102")
    for text, number, kind in [("Water is rising by the electrical panel", "+12315550101", "emergency"),
                               ("Please transfer me to a real person", "+12315550102", "human_transfer")]:
        session = core.create_session(kind)
        reply = await core.process(session, text)
        assert reply.transfer_number == number
        assert core.notifier.sent[-1][0] == kind
        assert text in core.database.get_call(session.call_id)["snapshot"]["description"]


@pytest.mark.parametrize("text", ["Are you a real person?", "Are you human?", "Am I speaking with a computer?"])
@pytest.mark.asyncio
async def test_identity_answer_is_truthful_and_is_not_a_transfer_request(core, text, monkeypatch):
    monkeypatch.setattr(core.settings, "live_transfer_number", "+12315550102")
    reply = await core.process(core.create_session("identity"), text)
    assert "AI phone assistant" in reply.text and not reply.transfer_number


def test_unknown_property_and_contact_data_are_not_invented():
    state = IntakeState(call_uuid="grounding")
    capture_volunteered(state, "I hope you can help. Quote 12345 is about mold in the basement.")
    assert not state.property_context and not state.name and not state.phone and not state.address
    assert state.affected_area


@pytest.mark.parametrize("text", ["Mainly damp walls, no standing water", "Humidity without standing water", "It is not standing water, just dampness", "We don't have standing water", "There isn't any standing water", "No visible standing water", "I'm not seeing standing water"])
def test_negative_standing_water_is_not_an_emergency(text):
    assert not detect_emergency(text)


def test_negative_standing_water_does_not_hide_a_real_hazard():
    assert detect_emergency("No standing water but the electrical panel is sparking")
    assert detect_emergency("There is standing water in my basement")


@pytest.mark.parametrize("intro", ["This is Sam Jones", "I'm Sam Jones", "My name is Sam Jones"])
def test_common_self_introductions_are_captured_without_guessing(intro):
    state = IntakeState(call_uuid="intro")
    capture_volunteered(state, intro + ". I need help with mold in my house.")
    assert state.name == "Sam Jones" and state.property_context == "Residential property"


@pytest.mark.parametrize("place", ["museum", "restaurant", "warehouse", "school", "church", "hotel"])
def test_explicit_managed_property_context_is_retained(place):
    state = IntakeState(call_uuid="property")
    capture_volunteered(state, f"The basement at our {place} is damp")
    assert state.property_context == "Commercial or managed property"


def test_contact_text_does_not_become_a_property_type():
    state = IntakeState(call_uuid="literal-contact")
    capture_volunteered(state, "My email is house at example dot com. This is for our office basement.")
    assert state.property_context == "Commercial or managed property"


def test_intro_and_problem_in_one_sentence_are_both_captured():
    state = IntakeState(call_uuid="same-sentence")
    capture_volunteered(state, "My name is Sam Jones and my basement gets damp every time it rains")
    assert state.name == "Sam Jones" and state.affected_area and state.timing_summary and state.source_summary


@pytest.mark.parametrize("timing", ["for two months", "for a few weeks", "for 3 days", "every spring", "when it rains", "started Monday", "since last week"])
@pytest.mark.asyncio
async def test_known_timing_is_not_asked_again(core, timing):
    session = core.create_session("known-timing")
    reply = await core.process(session, f"There has been mold on the basement wall {timing}. No safety concerns.")
    assert session.state.timing_summary and session.state.stage == "name"
    assert "when" not in reply.text.lower()


@pytest.mark.parametrize("last_message", ["Yes, please note the back door is locked", "No, please have them call after lunch"])
@pytest.mark.asyncio
async def test_extra_details_in_final_answer_are_saved_instead_of_discarded(core, last_message):
    session = core.create_session("final-details", "+12315550100")
    for text in ("Leave a message for Jim about the vendor audit", "Sam Jones", "yes", "yes"):
        await core.process(session, text)
    reply = await core.process(session, last_message)
    assert reply.end_call and last_message in session.state.description
    assert last_message in core.database.get_call(session.call_id)["snapshot"]["description"]


@pytest.mark.asyncio
async def test_several_contacts_in_a_name_answer_are_captured_separately(core):
    session = core.create_session("combined-contacts")
    session.state.stage = "name"
    memory(session.state)["kind"] = "message"
    await core.process(session, "My name is Sam Jones, my phone number is 2315550199, and my email is sam at example dot com")
    assert session.state.name == "Sam Jones"
    assert session.state.phone == "+12315550199" and session.state.email == "sam@example.com"
    assert session.state.stage == "confirm_name"
    await core.process(session, "yes")
    assert session.state.stage == "confirm_phone"


@pytest.mark.asyncio
async def test_unanswered_question_does_not_become_the_callers_name(core):
    session = core.create_session("unknown-question")
    session.state.stage = "name"
    reply = await core.process(session, "What color are the company trucks?")
    assert not session.state.name and session.state.stage == "name"
    assert "team will need to confirm" in reply.text


@pytest.mark.asyncio
async def test_final_question_is_answered_before_closing(core):
    session = core.create_session("closing-question", "+12315550100")
    for text in ("Leave a message for Jim about the vendor audit", "Sam Jones", "yes", "yes"):
        await core.process(session, text)
    reply = await core.process(session, "Is the estimate free?")
    assert not reply.end_call and "free inspections" in reply.text
    assert session.state.stage == "anything_else"
    assert (await core.process(session, "No, that's all")).end_call


@pytest.mark.asyncio
async def test_late_callback_change_is_confirmed_then_closes_without_another_wrapup(core):
    session = core.create_session("late-phone", "+12315550100")
    for text in ("Leave a message for Jim about the vendor audit", "Sam Jones", "yes", "yes"):
        await core.process(session, text)
    reply = await core.process(session, "Call me at 2315550199")
    assert not reply.end_call and session.state.stage == "confirm_phone"
    reply = await core.process(session, "yes")
    assert reply.end_call and session.state.phone == "+12315550199"


@pytest.mark.asyncio
async def test_unclear_phone_and_address_are_bounded_and_preserve_partial_message(core):
    session = core.create_session("unclear")
    session.state.stage = "phone"
    session.state.name = "Sam Jones"
    session.state.confirmations["name"] = "Sam Jones"
    for _ in range(2):
        await core.process(session, "garbled")
    assert session.state.stage == "address" and session.state.confirmations["phone"] == "unavailable"
    for _ in range(2):
        await core.process(session, "unclear")
    assert session.state.stage == "email" and session.state.confirmations["address"] == "unavailable"
    await core.process(session, "No email")
    reply = await core.process(session, "No, that's it")
    assert reply.end_call and not session.state.completed
    assert core.notifier.sent[-1] == ("completed_intake", True)


@pytest.mark.asyncio
async def test_unconfirmed_contact_is_kept_for_review_instead_of_becoming_a_saved_fact(core):
    session = core.create_session("unconfirmed")
    session.state.stage = "confirm_address"
    session.state.address = "20 Example Road, Ludington Michigan 49431"
    session.state.service_area_status, session.state.service_area_city = "published", "Ludington"
    for _ in range(2):
        await core.process(session, "unclear")
    assert not session.state.address
    assert session.state.metadata["unconfirmed_address"].startswith("20 Example Road")
    assert session.state.service_area_status == "unknown" and not session.state.service_area_city


@pytest.mark.asyncio
async def test_prepared_contextual_reply_does_not_wait_for_model_rewrite(core):
    session = core.create_session("fast-planned")
    reply = await core.process(session, "My basement gets damp every time it rains")
    phone = TelAgentPhoneAgent(core, None, None, None, None)
    text = "".join([piece async for piece in phone._reply_stream(session, "opening", reply, "issue")])
    assert text == reply.text == "Are you seeing standing water, or mainly dampness?"


@pytest.mark.asyncio
async def test_followups_are_bounded_for_an_unknown_request(core):
    session = core.create_session("bounded")
    await core.process(session, "I would like an estimate")
    for _ in range(4):
        if session.state.stage not in {"name", "confirm_name"}:
            await core.process(session, "I don't know")
    assert session.state.stage == "name"
    asked = memory(session.state)["asked"]
    assert len([stage for stage in asked if stage != "safety_summary"]) <= 2
