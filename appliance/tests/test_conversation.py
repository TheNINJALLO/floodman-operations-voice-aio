from __future__ import annotations

import pytest

from app.business import BusinessDirectory
from app.config import Settings
from app.conversation import capture_volunteered, closing_finished, contact_fields, memory, request_kind
from app.db import Database
from app.knowledge import KnowledgeBase
from app.intake import detect_emergency, detect_flood_emergency
from app.models import IntakeState
from app.voice_core import VoiceCore
from app.tel_agent_phone import TelAgentPhoneAgent


@pytest.mark.parametrize("text", [
    "THAT SHOULD BE IT", "That should be all.", "That's everything.",
    "No, I think that's all, thank you.", "I think that is it.",
    "No, that's all for now.", "I'm all set, thanks.", "No thanks.",
    "I don't have anything else.", "That is all.", "No, nothing more.",
    "No, that'll be all.", "That should do it.", "I don't think so.",
    "That's all I can think of.",
])
@pytest.mark.parametrize("stage", ["anything_else", "additional_message"])
@pytest.mark.asyncio
async def test_natural_closing_answers_finish_without_another_question(core, text, stage):
    session = core.create_session("natural-closing")
    session.state.stage = stage
    session.state.description = "Dampness at the perimeter and a floor crack."
    reply = await core.process(session, text)
    assert reply.end_call and reply.text.endswith("Goodbye.")
    assert session.state.stage == "done"
    assert "anything else" not in reply.text.lower()
    assert session.state.description == "Dampness at the perimeter and a floor crack."
    assert len(core.notifier.sent) == 1


@pytest.mark.parametrize("text", [
    "No, just ask Jim to call after lunch.", "That's all I know about the water.",
    "That should be it, but please use the side door.", "Nothing else except a crack in the floor.",
    "I think that's all but my phone number changed.", "Not yet.", "Yes.",
    "No water is rising.", "All but it seems like it's just around the perimeter and a crack on the floor.",
])
def test_closing_detection_never_matches_inside_additional_details(text):
    assert not closing_finished(text)


@pytest.mark.asyncio
async def test_reported_call_details_then_that_should_be_it_closes_once(core):
    session = core.create_session("reported-closing-loop")
    session.defer_assistant_recording = True
    session.state.stage = "anything_else"
    detail = "It seems like it's just around the perimeter and a crack on the floor."
    reply = await core.process(session, detail)
    assert not reply.end_call and detail in session.state.description
    reply = await core.process(session, "THAT SHOULD BE IT")
    assert reply.end_call and not core.notifier.sent
    await core.finalize_completion(session)
    await core.finalize_completion(session)
    assert len(core.notifier.sent) == 1
    assert "THAT SHOULD BE IT" not in session.state.description


@pytest.mark.asyncio
async def test_natural_email_then_requested_spelling_still_requires_confirmation(core):
    session = core.create_session("natural-email")
    session.state.stage = "email"
    reply = await core.process(session, "alex dot smith two at gmail dot com")
    assert session.state.email == "alex.smithtwo@gmail.com"  # Preserve capture behavior.
    assert "a, l, e, x, dot, s, m, i, t, h, t, w, o, at gmail dot com" in reply.text.lower()
    assert reply.speech_part_speeds == (1.0, None)
    reply = await core.process(session, "Could you spell that out?")
    assert "a, l, e, x, dot, s, m, i, t, h, t, w, o, at gmail dot com" in reply.text.lower()
    assert session.state.stage == "confirm_email" and "email" not in session.state.confirmations
    reply = await core.process(session, "No, it's alex dot smith2 at example dot com")
    assert session.state.email == "alex.smith2@example.com"
    assert "a, l, e, x, dot, s, m, i, t, h, two, at example dot com" in reply.text.lower()
    assert "email" not in session.state.confirmations
    await core.process(session, "Yes")
    assert session.state.confirmations["email"] == "alex.smith2@example.com"


@pytest.mark.parametrize("text, context, expected", [
    ("Water is rising by the electrical panel", "", True),
    ("My basement is flooding right now", "", True),
    ("Our house is flooded", "", True),
    ("I cannot stop the water", "", True),
    ("It is still coming in", "Water in the basement", True),
    ("It is still rising", "Water in the basement", True),
    ("My basement is not flooding", "", False),
    ("No water is rising", "", False),
    ("The water isn't rising", "", False),
    ("My basement was flooded last spring", "", False),
    ("My basement flooded yesterday", "", False),
    ("My basement is flooded but the water is off", "", False),
    ("It has stopped", "Water is rising in the basement", False),
    ("Only dampness", "Water in the basement", False),
    ("There is standing water in my basement", "", False),
    ("My electrical panel is sparking", "", False),
    ("My basement has a musty odor", "", False),
    ("Please transfer me to a human", "", False),
    ("Do you respond to flood emergencies?", "", False),
    ("What if my basement is flooding?", "", False),
    ("Is water still coming in?", "", False),
])
def test_active_flood_handoff_requires_reported_current_flooding(text, context, expected):
    assert detect_flood_emergency(text, context=context) is expected


@pytest.mark.asyncio
async def test_partial_closing_sentence_waits_and_saves_the_continuation(core):
    session = core.create_session("closing-fragment")
    session.state.stage = "anything_else"
    session.state.description = "Water in the basement."
    reply = await core.process(session, "Um, it's been...")
    assert reply.wait_for_caller and not reply.end_call
    assert not session.state.completed and not core.notifier.sent
    reply = await core.process(session, "happening every time it rains for two weeks.")
    assert not reply.end_call
    assert "it's been happening" in session.state.description
    assert session.state.stage == "anything_else"
    assert (await core.process(session, "No, that's all")).end_call


@pytest.mark.asyncio
async def test_short_hesitation_cannot_consume_a_detail_or_name(core):
    session = core.create_session("hesitation")
    await core.process(session, "Water in my basement")
    assert session.state.stage == "water_condition"
    assert (await core.process(session, "Oh.")).wait_for_caller
    assert session.state.stage == "water_condition" and not session.state.name
    await core.process(session, "A little bit of both.")
    assert not session.state.name


@pytest.mark.asyncio
async def test_interrupted_introduction_is_recovered_before_availability_prompt(core):
    session = core.create_session("intro-recovery")
    session.introduction_pending = True
    reply = await core.no_input(session)
    assert reply.text == core.greeting()
    assert session.no_input_count == 0 and not session.introduction_pending
    session.introduction_pending = True
    reply = await core.process(session, "Hello")
    assert "Floodman" in reply.text and "Avery" in reply.text
    assert not session.state.description and not reply.end_call


@pytest.mark.asyncio
async def test_real_opening_details_are_preserved_when_introduction_was_interrupted(core):
    session = core.create_session("intro-with-details")
    session.introduction_pending = True
    reply = await core.process(session, "Water in my basement")
    assert reply.text.startswith("You're through to Floodman")
    assert session.state.description == "Water in my basement"
    assert session.state.stage == "water_condition"


@pytest.mark.asyncio
async def test_answer_to_water_status_can_authorize_flood_emergency_handoff(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    session = core.create_session("water-status")
    assert not (await core.process(session, "Water in my basement")).transfer_number
    reply = await core.process(session, "It is still coming in")
    assert reply.transfer_number == "+12315550101"
    assert core.notifier.sent == [("emergency", True)]


@pytest.mark.asyncio
async def test_non_flood_hazard_alert_does_not_authorize_live_transfer(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    session = core.create_session("hazard-alert")
    reply = await core.process(session, "My electrical panel is sparking")
    assert not reply.transfer_number and "911" in reply.text
    assert core.notifier.sent == [("emergency", True)]


@pytest.mark.asyncio
async def test_interrupted_farewell_can_resume_and_keep_new_details(core):
    session = core.create_session("farewell-correction")
    session.state.stage = "anything_else"
    assert (await core.process(session, "No, that's all")).end_call
    reply = await core.process(session, "Wait, please have Jim call after lunch.")
    assert not reply.end_call and session.state.stage == "anything_else"
    assert "call after lunch" in session.state.description


@pytest.mark.asyncio
async def test_phone_completion_alert_waits_for_corrections_before_final_delivery(core):
    session = core.create_session("final-delivery")
    session.defer_assistant_recording = True
    session.state.stage = "anything_else"
    assert (await core.process(session, "No, that's all")).end_call
    assert not core.notifier.sent
    await core.process(session, "Wait, please have Jim call after lunch")
    await core.finalize_completion(session)
    assert not core.notifier.sent
    await core.process(session, "That's all")
    await core.finalize_completion(session)
    await core.finalize_completion(session)
    assert core.notifier.sent == [("completed_intake", True)]
    assert "call after lunch" in core.database.get_call(session.call_id)["snapshot"]["description"]


@pytest.mark.asyncio
async def test_phone_hangup_during_farewell_still_delivers_saved_message(core):
    session = core.create_session("farewell-disconnect")
    session.defer_assistant_recording = True
    session.state.stage = "anything_else"
    await core.process(session, "No, that's all")
    await core.disconnect(session)
    assert core.notifier.sent == [("completed_intake", True)]


@pytest.mark.asyncio
async def test_yes_okay_confirms_a_contact_instead_of_waiting(core):
    session = core.create_session("yes-okay")
    session.state.name = "Sam Jones"
    session.state.stage = "confirm_name"
    reply = await core.process(session, "Yes, OK.")
    assert not reply.wait_for_caller
    assert session.state.confirmations["name"] == "Sam Jones"


@pytest.mark.asyncio
async def test_partial_name_introduction_preserves_the_following_name(core):
    session = core.create_session("name-fragment")
    session.state.stage = "name"
    assert (await core.process(session, "My name is...")).wait_for_caller
    await core.process(session, "Sam Jones")
    assert session.state.name == "Sam Jones" and session.state.stage == "confirm_name"


@pytest.mark.asyncio
async def test_flood_report_split_across_a_pause_still_authorizes_handoff(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    session = core.create_session("split-flood")
    assert (await core.process(session, "The water is...")).wait_for_caller
    reply = await core.process(session, "rising")
    assert reply.transfer_number == "+12315550101"


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
@pytest.mark.parametrize("field,value,next_stage", [
    ("address", "99 Wrong Road, Ludington MI 49431", "address"),
    ("phone", "+12315550100", "phone"), ("name", "Sam Jones", "name"),
    ("email", "sam@example.com", "email"),
])
async def test_no_rejects_a_confirmation_instead_of_declining_the_field(core, field, value, next_stage):
    session = core.create_session("negative-confirmation")
    session.state.stage = "confirm_" + field
    setattr(session.state, field, value)
    reply = await core.process(session, "No.")
    assert session.state.stage == next_stage and not reply.end_call
    assert field not in memory(session.state)["declined"]
    assert field not in session.state.confirmations
    assert not getattr(session.state, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["No", "That's wrong", "That's not right", "That is not correct", "Nah"])
async def test_wrong_address_cannot_advance_to_email(core, answer):
    session = core.create_session("wrong-address")
    session.state.stage = "confirm_address"
    session.state.address = "99 Wrong Road, Ludington MI 49431"
    session.state.service_area_status, session.state.service_area_city = "published", "Ludington"
    await core.process(session, answer)
    assert session.state.stage == "address" and not session.state.address
    assert session.state.service_area_status == "unknown" and not session.state.service_area_city
    await core.process(session, "eight eight zero five East Example Street Ludington Michigan 49431")
    assert session.state.stage == "confirm_address"
    assert session.state.address == "8805 East Example Street Ludington Michigan 49431"
    assert "address" not in session.state.confirmations


@pytest.mark.asyncio
async def test_repeatedly_rejected_address_is_flagged_unavailable_and_never_confirmed(core):
    session = core.create_session("address-loop-break")
    session.state.stage = "confirm_address"
    session.state.address = "99 Wrong Road"
    await core.process(session, "No")
    await core.process(session, "98 Wrong Road")
    reply = await core.process(session, "No")
    assert not session.state.address and session.state.confirmations["address"] == "unavailable"
    assert session.state.metadata["unconfirmed_address"] == "98 Wrong Road"
    assert "team to confirm" in reply.text and not reply.end_call


@pytest.mark.asyncio
async def test_address_correction_after_wrapup_is_reconfirmed_and_does_not_reask_wrapup(core):
    session = core.create_session("late-address-correction")
    state = session.state
    state.name, state.phone, state.address = "Sam Jones", "+12315550100", "99 Wrong Road"
    state.confirmations.update(name=state.name, phone=state.phone, address=state.address)
    memory(state)["declined"].append("email")
    state.stage = "anything_else"
    reply = await core.process(session, "The building address is eight eight zero five East Example Street")
    assert state.stage == "confirm_address" and "address" not in state.confirmations
    assert state.address.startswith("8805 East Example Street") and not reply.end_call
    reply = await core.process(session, "Yes")
    assert reply.end_call and "anything else" not in reply.text.lower()


@pytest.mark.asyncio
async def test_first_name_prompts_for_last_name_once_and_confirms_the_combined_name(core):
    session = core.create_session("missing-surname", "+12315550100")
    session.state.stage = "name"
    memory(session.state)["kind"] = "message"
    reply = await core.process(session, "Sam")
    assert session.state.stage == "last_name" and "last name" in reply.text.lower()
    assert "name" not in session.state.confirmations
    reply = await core.process(session, "My last name is Van Buren.")
    assert session.state.stage == "confirm_name" and session.state.name == "Sam Van Buren"
    assert "Sam Van Buren" in reply.text
    await core.process(session, "Yes")
    assert session.state.confirmations["name"] == "Sam Van Buren"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["I'd rather not", "I don't want to give that", "I only use one name"])
async def test_missing_surname_is_not_invented_or_asked_for_in_a_loop(core, answer):
    session = core.create_session("surname-refusal")
    session.state.stage = "name"
    await core.process(session, "Sam")
    reply = await core.process(session, answer)
    assert session.state.stage == "confirm_name" and session.state.name == "Sam"
    assert memory(session.state)["last_name_unavailable"]
    assert "last name" not in reply.text.lower()


@pytest.mark.asyncio
async def test_wrapup_question_appears_once_while_details_questions_and_hold_remain_open(core):
    session = core.create_session("wrapup-once", "+12315550100")
    memory(session.state)["kind"] = "message"
    session.state.name = "Sam Jones"
    session.state.confirmations.update(name="Sam Jones", phone=session.state.phone)
    first = core._next_reply(session)
    replies = [first.text]
    assert first.text.count("anything else") == 1
    for text in ["Yes", "Please use the side door", "And call after lunch", "Is the estimate free?"]:
        reply = await core.process(session, text)
        replies.append(reply.text)
        assert not reply.end_call
    assert sum(text.lower().count("anything else") for text in replies) == 1
    assert replies[2] != replies[3]  # Different short acknowledgments.
    assert "side door" in session.state.description and "after lunch" in session.state.description
    await core.process(session, "Please wait")
    assert (await core.no_input(session)).wait_for_caller
    assert (await core.no_input(session)).wait_for_caller
    reply = await core.process(session, "That's all")
    assert reply.end_call and "take care" in reply.text and "Sam" in reply.text


@pytest.mark.asyncio
async def test_silence_after_an_extra_detail_gives_a_cue_then_closes_without_another_question(core):
    session = core.create_session("wrapup-silence")
    session.state.stage = "anything_else"
    await core.process(session, "Please use the side door")
    cue = await core.no_input(session)
    assert not cue.end_call and "clarify" in cue.text and "anything else" not in cue.text.lower()
    reply = await core.no_input(session)
    assert reply.end_call and "anything else" not in reply.text.lower()


@pytest.mark.asyncio
async def test_active_flooding_still_takes_priority_over_a_wrapup_correction(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    session = core.create_session("wrapup-emergency")
    session.state.stage = "anything_else"
    reply = await core.process(session, "Actually my basement is flooding right now and the water is rising.")
    assert reply.transfer_number == "+12315550101"


@pytest.mark.asyncio
async def test_opening_details_skip_property_room_timing_and_source_questions(core):
    session = core.create_session("rich-opening")
    reply = await core.process(session, "My name is Sam Jones. The basement at my house gets damp every time it rains. No safety concerns.")
    assert session.state.name == "Sam Jones"
    assert session.state.property_context == "Residential property"
    assert "basement" in session.state.affected_area
    assert session.state.timing_summary and session.state.source_summary and session.state.safety_summary
    assert session.state.stage == "water_condition"
    assert reply.text == "Is water still coming in or rising, or has it stopped?"
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
    assert session.state.stage == "name"


@pytest.mark.asyncio
async def test_no_hazard_question_and_no_invented_safety_answer(core):
    session = core.create_session("no-hazard-checklist")
    reply = await core.process(session, "There is mold on my bathroom wall. I noticed it yesterday.")
    assert session.state.stage == "name"
    assert session.state.safety_summary == ""
    assert all(word not in reply.text.lower() for word in ("sewage", "electrical", "hazard", "unsafe"))
    assert "safety_summary" not in memory(session.state)["asked"]


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
async def test_only_active_flooding_preempts_intake_with_live_transfer(core, monkeypatch):
    monkeypatch.setattr(core.settings, "emergency_transfer_number", "+12315550101")
    monkeypatch.setattr(core.settings, "live_transfer_number", "+12315550102")
    for text, number, kind in [("Water is rising by the electrical panel", "+12315550101", "emergency")]:
        session = core.create_session(kind)
        reply = await core.process(session, text)
        assert reply.transfer_number == number
        assert core.notifier.sent[-1][0] == kind
        assert text in core.database.get_call(session.call_id)["snapshot"]["description"]

    session = core.create_session("human-callback")
    reply = await core.process(session, "Please transfer me to a real person")
    assert not reply.transfer_number and not reply.end_call
    assert "call you back" in reply.text
    assert session.state.stage == "service_detail"
    await core.process(session, "Please ask Jim to call about my invoice")
    assert session.state.stage == "name" and not session.state.address
    assert core.notifier.sent == [("emergency", True)]


@pytest.mark.parametrize("text", ["Are you a real person?", "Are you human?", "Am I speaking with a computer?"])
@pytest.mark.asyncio
async def test_identity_answer_is_truthful_and_is_not_a_transfer_request(core, text, monkeypatch):
    monkeypatch.setattr(core.settings, "live_transfer_number", "+12315550102")
    reply = await core.process(core.create_session("identity"), text)
    assert "digital assistant" in reply.text and not reply.transfer_number


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
    assert not reply.end_call and last_message in session.state.description
    assert (await core.process(session, "That's all")).end_call
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
    assert text == reply.text == "Is water still coming in or rising, or has it stopped?"


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
