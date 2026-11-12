"""Regression coverage for question turns recorded on the real phone path."""
import pytest

from app.company_conversation import estimate_inquiry, is_question
from app.conversation import memory
from app.streaming_stt import needs_conversation_recovery
from test_conversation import core


ESTIMATE_QUESTIONS = [
    "Do you do estimates",
    "You guys do estimates",
    "Do you offer free estimates",
    "Free estimates",
    "OH BEARDS DO PRE ESTIMATES",  # Actual streaming transcript, not corrected text.
    "I'VE JUST CARES IF YOU GUISE DO ESTIMATES",
    "I'm just curious if you give estimates",
    "Oh, do you offer inspections",
]


@pytest.mark.parametrize("text", ESTIMATE_QUESTIONS)
def test_unpunctuated_estimate_inquiries_are_recognized(text):
    assert estimate_inquiry(text) and is_question(text)


@pytest.mark.parametrize("text", [
    "Oh, do you help with foundation repair",
    "Thanks, what services do you offer",
    "I'm just curious whether you work with commercial properties",
    "I was wondering if you repair foundations",
    "I just wanted to know how waterproofing works",
])
def test_questions_can_have_natural_introductions(text):
    assert is_question(text)


@pytest.mark.parametrize("text", [
    "I need an estimate", "Our inspection was yesterday", "My estimate number is 123",
    "Sam Jones", "Will Smith", "estimates@example.com", "123 Estimate Street",
])
def test_service_requests_and_contact_values_are_not_offer_questions(text):
    assert not estimate_inquiry(text) and not is_question(text)


@pytest.mark.parametrize("stage", [
    "issue", "service_detail", "affected_area", "name", "last_name", "phone",
    "address", "email", "confirm_address", "confirm_email", "anything_else", "additional_message",
])
@pytest.mark.parametrize("text", ESTIMATE_QUESTIONS)
@pytest.mark.asyncio
async def test_estimate_inquiry_answers_immediately_without_advancing_or_capturing_fields(core, stage, text):
    class NoGeneration:
        async def answer_in_conversation(self, *args):
            raise AssertionError("The approved estimate offer must answer without inference")
    core.llm = NoGeneration()
    session = core.create_session("estimate-question-" + stage)
    session.state.stage = stage
    if stage != "issue":
        session.state.description = "Damp basement after rain."
    before = {field: getattr(session.state, field) for field in (
        "stage", "description", "name", "phone", "address", "email", "email_status",
    )}
    reply = await core.process(session, text)
    assert "free inspections and consultations" in reply.text
    assert "pricing depend" in reply.text and not reply.end_call and not reply.transfer_number
    assert "?" not in reply.text
    assert before == {field: getattr(session.state, field) for field in before}
    assert not core.notifier.sent


@pytest.mark.asyncio
async def test_caller_can_ask_about_estimates_after_a_message_request(core):
    session = core.create_session("message-then-company-question")
    memory(session.state)["kind"] = "message"
    reply = await core.process(session, "Do you do estimates")
    assert "free inspections" in reply.text and session.state.stage == "issue"
    assert memory(session.state)["kind"] == "message"


@pytest.mark.asyncio
async def test_question_introduced_as_a_story_is_answered_before_resuming_name(core):
    session = core.create_session("question-not-a-story")
    session.state.stage = "name"
    session.state.description = "Damp basement after rain."
    reply = await core.process(session, "I'm just curious if you offer free estimates for my basement")
    assert "free inspections" in reply.text and not session.state.name
    assert session.state.description == "Damp basement after rain."
    reply = await core.process(session, "Sam Jones")
    assert session.state.name == "Sam Jones" and session.state.stage == "confirm_name"


@pytest.mark.asyncio
async def test_intake_still_starts_when_the_caller_explicitly_requests_an_estimate(core):
    session = core.create_session("estimate-request")
    reply = await core.process(session, "Can I get a free estimate for my damp basement")
    assert "free inspections" in reply.text and session.state.description
    assert session.state.stage != "issue" and not reply.end_call


@pytest.mark.parametrize("stage,text", [
    ("issue", "OH BEARDS DO PRE ESTIMATES"),
    ("service_detail", "You guys do estimates"),
    ("affected_area", "I'VE JUST CARES IF YOU GUISE DO ESTIMATES"),
    ("issue", "Do you offer inspections"),
    ("issue", "Do you offer consultations"),
])
def test_recognizable_plural_offer_words_avoid_the_slow_secondary_decode(stage, text):
    assert not needs_conversation_recovery(stage, text)
