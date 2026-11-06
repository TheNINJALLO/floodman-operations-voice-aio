import pytest

from app.company_conversation import context_summary, is_question, search_query, supportive_opening, safe_answer
from app.conversation import memory
from app.conversation import unfinished_turn
from app.knowledge import KnowledgeBase
from app.llm import LocalLLM
from test_conversation import core


class ContextModel:
    def __init__(self): self.calls = []
    async def answer_in_conversation(self, question, approved, recent):
        self.calls.append((question, approved, recent))
        return "We can inspect the moisture conditions and explain the options for your property."


@pytest.mark.parametrize("text", ["Tell me about Floodman", "Does that help with the smell too", "What about commercial properties", "I'm worried, what does waterproofing involve", "How does that work"])
def test_conversational_company_questions_do_not_require_punctuation(text):
    assert is_question(text, has_topic=True)


@pytest.mark.parametrize("text", ["Sam Jones", "Will Smith", "My last name is Jones", "123 Main Street", "No", "sam@example.com"])
def test_contact_answers_are_not_company_questions(text):
    assert not is_question(text, has_topic=True)


@pytest.mark.asyncio
async def test_company_question_gets_conversation_context_and_preserves_intake_stage(core):
    model = ContextModel()
    core.llm = model
    # This test exercises generation, independently of the immediate FAQ path.
    core.knowledge.conversation_answer = lambda *args, **kwargs: ""
    session = core.create_session("company-context")
    session.state.stage = "name"
    session.state.description = "Damp crawl space after rain."
    first = await core.process(session, "How does crawl space encapsulation work")
    assert not first.end_call and session.state.stage == "name" and not session.state.name
    assert "name" not in first.text.lower()
    await core.process(session, "Would that help with moisture too")
    assert len(model.calls) == 2
    assert "crawl" in model.calls[1][1].lower()
    assert "How does crawl space encapsulation work" in model.calls[1][2]
    assert "Damp crawl space after rain" in model.calls[1][2]
    await core.process(session, "Sam Jones")
    assert session.state.name == "Sam Jones" and session.state.stage == "confirm_name"


@pytest.mark.asyncio
async def test_company_question_does_not_start_collecting_contacts_just_because_it_mentions_estimate(core):
    session = core.create_session("free-estimate-info")
    reply = await core.process(session, "Is the estimate free?")
    assert "free inspections" in reply.text and session.state.stage == "issue"
    assert reply.text.count("?") == 0 and not reply.end_call


@pytest.mark.asyncio
async def test_unknown_question_does_not_send_irrelevant_docs_to_model_or_become_a_name(core):
    model = ContextModel()
    core.llm = model
    session = core.create_session("unknown-company-question")
    session.state.stage = "name"
    reply = await core.process(session, "What color are your trucks?")
    assert not model.calls and not session.state.name and session.state.stage == "name"
    assert "team will need to confirm" in reply.text


def test_new_service_topic_does_not_inherit_previous_service():
    dialogue = {"topic": "foundation", "turns": [{"question": "How does foundation repair work?", "answer": "An inspection is needed."}]}
    assert search_query("What about mold remediation?", dialogue) == "What about mold remediation?"


@pytest.mark.asyncio
async def test_question_history_is_per_call_and_bounded(core):
    first, other = core.create_session("questions-one"), core.create_session("questions-two")
    for _ in range(7): await core.process(first, "What services do you offer?")
    assert len(memory(first.state)["company_dialogue"]["turns"]) == 4
    assert not memory(other.state).get("company_dialogue")


@pytest.mark.parametrize("text", ["I'm really worried about my basement", "I feel overwhelmed", "I don't know what to do"])
def test_caring_acknowledgment_is_calm_and_has_no_cheerful_or_emotional_claim(text):
    assert supportive_opening(text) == "Let's take this one step at a time. "


def test_negated_worry_does_not_add_unneeded_empathy():
    assert not supportive_opening("I'm not worried, I just need a quote")


@pytest.mark.parametrize("text", ["I've booked your appointment.", "Your appointment is confirmed.", "You are verified.", "We will arrive in ten minutes.", "That will cost $500."])
def test_model_company_answers_cannot_claim_protected_actions_or_invent_prices(text):
    assert not safe_answer(text)


@pytest.mark.asyncio
async def test_local_answer_uses_approved_information_and_recent_context_without_tools(core, monkeypatch):
    model = LocalLLM(core.settings)
    captured = []
    async def chat(messages, **kwargs):
        captured.append(messages)
        return "An inspection helps the team understand the moisture conditions."
    monkeypatch.setattr(model, "_chat", chat)
    assert await model.answer_in_conversation("Does that help?", "Approved crawl-space information", "Caller asked about a crawl space")
    assert "professional, calm, gently caring" in captured[0][0]["content"]
    assert len(captured[0][0]["content"].split()) < 60
    assert "Approved crawl-space information" in captured[0][1]["content"]
    assert "Caller asked about a crawl space" in captured[0][1]["content"]
    async def unsafe(*_, **__): return "I've booked your appointment."
    monkeypatch.setattr(model, "_chat", unsafe)
    assert not await model.answer("Book it", "Some approved facts")


def test_cpu_context_keeps_question_and_concern_without_replaying_generated_answer():
    dialogue = {"topic": "crawl space", "turns": [{"question": "How does encapsulation work?", "answer": "Old generated answer. " * 40}]}
    recent = context_summary(dialogue, "Damp crawl space after rain.")
    assert "How does encapsulation work?" in recent and "Damp crawl space after rain." in recent
    assert "Old generated answer" not in recent and len(recent) <= 400


def test_knowledge_search_ignores_question_stopwords(project_root):
    knowledge = KnowledgeBase(project_root / "knowledge")
    assert not knowledge.search("What color are your trucks")
    assert knowledge.search("leaking basements")
    assert all(d.path.name != "99-review-needed.md" for d in knowledge.search("business experience company"))


def test_compact_approved_context_keeps_company_experience_and_is_bounded(project_root):
    knowledge = KnowledgeBase(project_root / "knowledge")
    context = knowledge.answer_context("Tell me about Floodman and how long you've been in business")
    assert "fifty years" in context.lower()
    assert len(context) < 1500


def test_process_question_retrieves_the_method_instead_of_a_general_service_list(project_root):
    knowledge = KnowledgeBase(project_root / "knowledge")
    context = knowledge.answer_context("How does crawl space encapsulation work")
    assert "vapor barrier" in context and "dehumidification" in context
    assert "Company Overview" not in context and len(context) < 500
    followup = knowledge.answer_context("Would that help with moisture and musty smells too crawl space How does crawl space encapsulation work", intent_query="Would that help with moisture and musty smells too")
    assert "musty odor" in followup


def test_immediate_company_answers_require_and_follow_approved_facts(project_root, tmp_path):
    assert not KnowledgeBase(tmp_path).direct_answer("How long have you been in business?")
    knowledge = KnowledgeBase(project_root / "knowledge")
    assert "fifty years" in knowledge.direct_answer("How long have you been in business?")
    assert "commercial property owners" in knowledge.direct_answer("Do you help commercial properties too?")
    assert not knowledge.direct_answer("How long does an inspection take?")


def test_service_answers_use_current_approved_documents_and_never_promise_a_fix(project_root, tmp_path):
    knowledge = KnowledgeBase(project_root / "knowledge")
    query = "How does crawl space encapsulation work"
    documents = knowledge.search(query)
    answer = knowledge.conversation_answer(query, intent_query=query, documents=documents)
    assert "vapor barrier" in answer and "may include" in answer
    followup = "Would that help with moisture and musty smells too"
    assert "musty odors" in knowledge.conversation_answer(query, intent_query=followup, documents=documents)
    assert not knowledge.conversation_answer(query, intent_query="Will it definitely fix the moisture forever?", documents=documents)
    assert not knowledge.conversation_answer(query, intent_query=query, documents=[])
    # Editing or withdrawing the approved facts disables the prepared answer.
    from dataclasses import replace
    changed = [replace(documents[0], body="The team must confirm all details.")]
    assert not knowledge.conversation_answer(query, intent_query=query, documents=changed)


@pytest.mark.asyncio
async def test_common_service_question_and_followup_do_not_wait_for_cpu_generation(core):
    class UnavailableModel:
        async def answer_in_conversation(self, *args):
            raise AssertionError("Common approved answers should not wait for the model")
    core.llm = UnavailableModel()
    session = core.create_session("immediate-service-answer")
    session.state.stage = "name"
    first = await core.process(session, "How does crawl space encapsulation work")
    second = await core.process(session, "Would that help with moisture and musty smells too")
    assert "vapor barrier" in first.text and "musty odors" in second.text
    assert session.state.stage == "name" and not session.state.name


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Tell me about your company", "What does an inspection involve?"])
async def test_missing_model_answer_keeps_a_company_question_out_of_intake_fields(core, text):
    session = core.create_session("company-fallback")
    reply = await core.process(session, text)
    assert session.state.stage == "issue" and not session.state.description and not session.state.name
    assert "team will need to confirm" in reply.text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Can I ask you a question?", "I have a question"])
async def test_conversational_opening_invites_the_question_without_collecting_a_field(core, text):
    session = core.create_session("question-opening")
    reply = await core.process(session, text)
    assert "What would you like to know?" in reply.text and session.state.stage == "issue"


@pytest.mark.parametrize("text", ["IT'S BEEN THE", "It's been a", "It has been an", "IT'S BEEN I", "It's been my", "It's been we"])
def test_short_recognized_fragment_with_a_determiner_is_still_unfinished(text):
    assert unfinished_turn(text)


def test_complete_sentence_with_a_determiner_is_not_left_waiting():
    assert not unfinished_turn("It's been the same way for two weeks")


@pytest.mark.asyncio
async def test_company_only_call_can_end_without_intake_or_a_false_callback_promise(core):
    session = core.create_session("questions-only")
    await core.process(session, "What services do you offer?")
    assert (await core.process(session, "Thanks")).text == "You're welcome."
    reply = await core.process(session, "That's all I needed. Goodbye.")
    assert reply.end_call and "within" not in reply.text and not session.state.completed
    assert not core.notifier.sent
    await core.disconnect(session, "completed")
    assert not core.notifier.sent


@pytest.mark.asyncio
async def test_interrupted_question_only_farewell_can_resume_an_actual_request(core):
    session = core.create_session("question-farewell-correction")
    await core.process(session, "What services do you offer?")
    assert (await core.process(session, "Goodbye")).end_call
    reply = await core.process(session, "Wait, I need an inspection for my damp basement.")
    assert not reply.end_call and session.state.description
    assert not memory(session.state).get("questions_only_complete")
