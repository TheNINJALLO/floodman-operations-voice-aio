import asyncio
from dataclasses import replace

import pytest

from app.call_style import extra_acknowledgment
from app.conversation import closing_finished, memory, unfinished_turn
from app.streaming_stt import StreamingLocalSTT, needs_conversation_recovery
from test_conversation import core


@pytest.mark.parametrize("text", [
    "Thank you so much", "Oh, thank you very much", "Thanks for your help",
    "I appreciate your help", "Okay, thanks a lot", "I THINK YOU SO MUCH",
])
@pytest.mark.asyncio
async def test_whole_gratitude_turn_finishes_wrapup_instead_of_becoming_another_note(core, text):
    session = core.create_session("grateful-closing")
    session.state.stage = "anything_else"
    session.state.description = "The caller would like an inspection."
    reply = await core.process(session, text)
    assert reply.end_call and "take care" in reply.text
    assert session.state.description == "The caller would like an inspection."


@pytest.mark.parametrize("text", ["Thanks, but the side door is locked", "Thank you and can you call after lunch", "I think you should check the basement", "Please tell them thanks for the work"])
def test_gratitude_inside_a_detail_is_not_a_hangup_signal(text):
    assert not closing_finished(text)


@pytest.mark.parametrize("stage", ["name", "last_name", "phone", "address", "email", "confirm_name", "confirm_address"])
@pytest.mark.asyncio
async def test_story_continuation_cannot_become_a_contact_value(core, stage):
    session = core.create_session("story-" + stage)
    session.state.stage = stage
    session.state.name = "Sam Jones" if stage.startswith("confirm_") else ""
    before = tuple(getattr(session.state, key) for key in ("name", "phone", "address", "email"))
    reply = await core.process(session, "There is mold on the basement wall and it started last week.")
    assert not reply.end_call and session.state.stage == stage
    assert tuple(getattr(session.state, key) for key in ("name", "phone", "address", "email")) == before
    assert "mold" in session.state.description and session.state.timing_summary


@pytest.mark.asyncio
async def test_feeling_worried_is_not_a_name_and_leaves_room_to_explain(core):
    session = core.create_session("worried-contact")
    session.state.stage = "name"
    reply = await core.process(session, "I'm really worried about this.")
    assert not session.state.name and session.state.stage == "name"
    assert "one step at a time" in reply.text and "listening" in reply.text
    reply = await core.process(session, "Sam Jones")
    assert session.state.name == "Sam Jones" and session.state.stage == "confirm_name"


@pytest.mark.parametrize("text", ["Let me finish", "I'm still talking", "Can I just explain", "Can I tell you what happened"])
@pytest.mark.asyncio
async def test_requests_to_keep_talking_preserve_the_pending_question(core, text):
    session = core.create_session("keep-talking")
    session.state.stage = "email"
    reply = await core.process(session, text)
    assert not reply.end_call and session.state.stage == "email" and "Take your time" in reply.text
    assert not session.state.metadata.get("email_capture_attempts")


@pytest.mark.asyncio
async def test_mid_intake_thanks_and_okay_do_not_advance_or_end_the_call(core):
    session = core.create_session("social-contact")
    session.state.stage = "name"
    assert (await core.process(session, "Thank you so much")).text == "You're welcome."
    assert (await core.process(session, "Okay")).wait_for_caller
    assert session.state.stage == "name" and not session.state.name


@pytest.mark.parametrize("detail,expected", [
    ("I just need a free estimate", "inspection"),
    ("Please use the side door", "side door"),
    ("Please call after lunch", "callback time"),
])
@pytest.mark.asyncio
async def test_extra_detail_gets_a_specific_reply_and_one_invitation_to_clarify(core, detail, expected):
    session = core.create_session("specific-extra")
    session.state.stage = "anything_else"
    reply = await core.process(session, detail)
    assert expected in reply.text and "clarify" in reply.text and "?" not in reply.text
    assert not reply.end_call and detail in session.state.description
    reply = await core.process(session, "The gate is unlocked.")
    assert not reply.end_call and "clarify" not in reply.text and "record" not in reply.text


@pytest.mark.asyncio
async def test_silence_gives_a_courtesy_cue_before_closing_and_new_details_reset_it(core):
    session = core.create_session("patient-wrapup")
    session.state.stage = "anything_else"
    await core.process(session, "Please use the side door")
    first = await core.no_input(session)
    assert not first.end_call and "clarify" in first.text and "?" not in first.text
    await core.process(session, "And the gate is unlocked")
    assert not (await core.no_input(session)).end_call
    assert (await core.no_input(session)).end_call


@pytest.mark.parametrize("text", ["There's mold in the", "I have", "It's on my", "And also"])
def test_pauses_in_an_unfinished_story_keep_the_floor_open(text):
    assert unfinished_turn(text)


@pytest.mark.parametrize("stage,text,expected", [
    ("issue", "AM I HAVE A CULL THE HOUSE", True),
    ("service_detail", "OLD", True),
    ("affected_area", "AHMAIN ONE OF THE MATTERS", True),
    ("issue", "There is mold in the bathroom", False),
    ("issue", "There is mould in the bathroom", False),
    ("anything_else", "I just need a free estimate", False),
    ("anything_else", "I THINK YOU SO MUCH", False),
    ("anything_else", "Please use the side door", False),
    ("anything_else", "I appreciate your help", False),
    ("water_condition", "It isn't rising", True),
    ("anything_else", "My email is sam@example.com", True),
])
def test_recognition_recovery_targets_unclear_speech_and_contact_corrections(stage, text, expected):
    assert needs_conversation_recovery(stage, text) is expected


@pytest.mark.asyncio
async def test_local_conversation_recovery_keeps_the_original_on_empty_or_error(core):
    stt = StreamingLocalSTT(core.settings)
    class Decoder:
        async def transcribe(self, pcm, rate, *, endpointed):
            assert endpointed and rate == 8000 and pcm == b"audio"
            return "There's mold in the bathroom."
    stt.fallback = Decoder()
    assert "mold" in await stt._recover_conversation("OLD", b"audio", "issue")
    async def empty(*args, **kwargs): return ""
    stt.fallback.transcribe = empty
    assert await stt._recover_conversation("Original words", b"audio", "issue") == "Original words"
    async def failed(*args, **kwargs): raise RuntimeError("decoder unavailable")
    stt.fallback.transcribe = failed
    assert await stt._recover_conversation("Original words", b"audio", "issue") == "Original words"


@pytest.mark.asyncio
async def test_slow_conversation_decoder_is_bounded_and_does_not_queue_more_work(core, monkeypatch):
    stt = StreamingLocalSTT(core.settings)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        await release.wait()
        return "Recovered words"
    stt.fallback.transcribe = slow
    original_timeout = asyncio.timeout
    monkeypatch.setattr("app.streaming_stt.asyncio.timeout", lambda seconds: original_timeout(0.01))
    try:
        assert await stt._recover_conversation("Original words", b"audio", "issue") == "Original words"
        await started.wait()
        assert not stt._conversation_recovery.done()
        assert await stt._recover_conversation("Next words", b"audio", "issue") == "Next words"
        assert len(calls) == 1
    finally:
        release.set()
        await stt._conversation_recovery


@pytest.mark.asyncio
async def test_stream_endpoint_recovers_a_garbled_problem_from_the_actual_captured_pcm(core, monkeypatch):
    from test_local_streaming_trial import FakeRecognizer
    class Gate:
        def __init__(self, *args): pass
        def probability(self, frame): return float(any(frame))
    monkeypatch.setattr("app.streaming_stt.SileroGate", Gate)
    stt = StreamingLocalSTT(replace(core.settings, sherpa_whisper_fallback=True))
    stt.recognizer = FakeRecognizer("OLD")
    captured = []
    class Decoder:
        _model = object()
        async def transcribe(self, pcm, sample_rate, *, endpointed):
            captured.append((pcm, sample_rate, endpointed))
            return "There's mold in the bathroom."
    stt.fallback = Decoder()
    voice = (1000).to_bytes(2, "little", signed=True) * 160
    async def frames():
        for frame in [voice] * 15 + [bytes(320)] * 45:
            yield frame
    turns = [turn async for turn in stt.stream_turns(frames(), get_stage=lambda: "issue")]
    assert len(turns) == 1 and "mold" in turns[0].text
    assert len(captured) == 1 and captured[0][1:] == (8000, True) and voice in captured[0][0]
