import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.audiosocket import AudioSocketConnection
from app.playback_echo import PlaybackEchoFilter
from app.speech_activity import SpeechActivity
from app.streaming_stt import StreamingLocalSTT
from test_audiosocket_protocol import BufferWriter, settings as transport_settings
from test_local_streaming_trial import FakeRecognizer


class Gate:
    def __init__(self, probability=.95):
        self.value = probability

    def probability(self, _payload):
        return self.value

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_normal_stream_timeout_waits_while_contact_recognition_is_busy(monkeypatch):
    from app.models import IntakeState, VoiceReply
    from app.tel_agent_phone import TelAgentPhoneAgent
    from app.tel_agent_runtime import TelAgentTranscriptTurn
    activity = SpeechActivity(decoding=True)
    class Connection:
        closed = False
        trace = None
        dropped_frames = 0
        close_reason = 'assistant_termination'
        speech_activity = activity
        async def start(self): return 'busy-contact-recognition'
        def event(self, *_args, **_kwargs): pass
        async def close(self): self.closed = True
    connection = Connection()
    session = SimpleNamespace(state=IntakeState(call_uuid='busy-contact-recognition', stage='last_name'), call_id=1)
    class Core:
        settings = SimpleNamespace(local_voice_stack='streaming_trial')
        def create_session(self, *_): return session
        async def call_started(self, *_): pass
        def greeting(self): return 'Hello from Avery.'
        def record_assistant(self, *_args, **_kwargs): pass
        async def no_input(self, *_): raise AssertionError('Recognition is still busy')
        async def process(self, _session, text, **_):
            assert text == 'Jones'
            return VoiceReply(text='Thank you.')
        async def disconnect(self, *_): pass
    monkeypatch.setattr('app.tel_agent_phone.AudioSocketConnection', lambda *_: connection)
    waits = []
    async def timeout_then_recognize(awaitable, **_):
        awaitable.close()
        waits.append(True)
        if len(waits) == 1:
            raise asyncio.TimeoutError
        activity.decoding = False
        return TelAgentTranscriptTurn(text='Jones', heard_audio=True)
    monkeypatch.setattr('app.tel_agent_phone.asyncio.wait_for', timeout_then_recognize)
    phone = TelAgentPhoneAgent(Core(), SimpleNamespace(streaming=True), None, None,
                             SimpleNamespace(read_pre=lambda *_: {}, write_action=lambda *_: None))
    async def pump(*_): await asyncio.Event().wait()
    async def speak(_connection, stream, *_args, **_kwargs):
        text = ''.join([part async for part in stream])
        if text == 'Thank you.': connection.closed = True
        return SimpleNamespace(text=text, interrupted=False, first_audio_ms=1)
    monkeypatch.setattr(phone, '_pump_transcripts', pump)
    monkeypatch.setattr(phone, '_speak', speak)
    await phone.handle(None, SimpleNamespace(get_extra_info=lambda *_: None))
    assert len(waits) == 2

def pcm(level, ms=20):
    return int(level).to_bytes(2, 'little', signed=True) * (8 * ms)


def connection():
    settings = transport_settings()
    settings.barge_in_min_speech_ms = 320
    settings.barge_in_energy_threshold = 500
    settings.barge_in_vad_threshold = .75
    settings.barge_in_max_gap_ms = 60
    result = AudioSocketConnection(asyncio.StreamReader(), BufferWriter(), settings)
    result._barge_gate = Gate()
    return result


@pytest.mark.parametrize('level', [128, 418, 499])
def test_high_vad_confidence_does_not_bypass_volume_floor(level):
    call = connection()
    for _ in range(50):
        call._capture_barge_in(pcm(level))
    assert not call.barge_in_event.is_set()


def test_uncertain_background_sound_does_not_interrupt():
    call = connection()
    call._barge_gate = Gate(.6)
    for _ in range(50):
        call._capture_barge_in(pcm(1400))
    assert not call.barge_in_event.is_set()


def test_short_clunks_do_not_accumulate_into_sustained_speech():
    call = connection()
    for _ in range(10):
        for _ in range(4):
            call._capture_barge_in(pcm(2000))
        for _ in range(5):
            call._barge_gate.value = 0
            call._capture_barge_in(pcm(0))
        call._barge_gate.value = .95
    assert not call.barge_in_event.is_set()


def test_sustained_caller_speech_interrupts_and_preserves_preroll():
    call = connection()
    for _ in range(15):
        call._capture_barge_in(pcm(1300))
    assert not call.barge_in_event.is_set()
    call._capture_barge_in(pcm(1300))
    assert call.barge_in_event.is_set()
    assert len(call.queue.get_nowait()) >= 16 * 320


def test_quiet_short_answer_is_retained_without_cutting_off_playback():
    call = connection()
    call.output_active = True
    call._capture_barge_in(pcm(180, 100))
    assert not call.barge_in_event.is_set()
    call._release_output()
    assert call.queue.get_nowait() == pcm(180, 100)


def test_echo_subtraction_retains_unrelated_overlapping_caller():
    rng = np.random.default_rng(18)
    reference = rng.normal(0, 1700, 16000).astype('<i2')
    caller = rng.normal(0, 600, 8000).astype('<i2')
    # Non-periodic input makes false correlation an explicit failure.
    echo = PlaybackEchoFilter()
    echo.record(reference.tobytes())
    incoming = .65 * reference[3000:11000].astype(np.float32)
    mixed = incoming + caller
    residual = []
    for index in range(0, len(mixed), 160):
        raw = np.clip(mixed[index:index + 160], -32768, 32767).astype('<i2').tobytes()
        residual.extend(np.frombuffer(echo.filter(raw), dtype='<i2'))
    residual = np.asarray(residual)[640:]
    assert np.corrcoef(residual, caller[640:])[0, 1] > .95
    assert np.sqrt(np.mean((residual - caller[640:]) ** 2)) < 140


def test_reference_echo_is_removed_but_unrelated_speech_is_not():
    rng = np.random.default_rng(10)
    reference = rng.normal(0, 1500, 16000).astype('<i2')
    for correlated in (True, False):
        echo = PlaybackEchoFilter()
        echo.record(reference.tobytes())
        incoming = (.6 * reference[4000:12000]).astype('<i2') if correlated else rng.normal(0, 1100, 8000).astype('<i2')
        output = []
        for offset in range(0, len(incoming), 160):
            output.extend(np.frombuffer(echo.filter(incoming[offset:offset + 160].tobytes()), dtype='<i2'))
        output = np.asarray(output, dtype=np.float64)[640:]
        if correlated:
            assert np.sqrt(np.mean(output ** 2)) < 20
        else:
            assert np.corrcoef(output, incoming[640:])[0, 1] > .99


def test_echo_state_is_per_call_and_expires(monkeypatch):
    one, two = PlaybackEchoFilter(), PlaybackEchoFilter()
    one.record(pcm(1200, 100))
    assert len(one.reference) and not len(two.reference)
    one.last_output_at = time.perf_counter() - 2
    assert one.filter(pcm(800)) == pcm(800)


def test_recovery_waits_for_current_speech_and_contact_decoding():
    activity = SpeechActivity()
    assert not activity.busy()
    activity.voiced()
    assert activity.busy()
    activity.last_voice_at -= 3
    assert not activity.busy()
    activity.decoding = True
    assert activity.busy()


@pytest.mark.asyncio
async def test_noisy_energy_below_learned_floor_cannot_extend_a_turn(tmp_path, monkeypatch):
    from app.config import Settings
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('SHERPA_WHISPER_FALLBACK', 'false')
    settings = Settings.from_env()
    monkeypatch.setattr('app.streaming_stt.SileroGate', lambda *_: Gate(.9))
    stt = StreamingLocalSTT(settings)
    stt.recognizer = FakeRecognizer('yes')
    activity = SpeechActivity(background_energy=400)
    async def frames():
        for _ in range(100):
            yield pcm(128)
    assert [turn async for turn in stt.stream_turns(frames(), activity=activity)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('caller_arrives', [False, True])
async def test_driver_recovers_empty_interruption_without_talking_over_a_real_turn(monkeypatch, caller_arrives):
    monkeypatch.setenv('TEL_AGENT_NATURALIZE', 'false')
    from app.models import IntakeState, VoiceReply
    from app.tel_agent_phone import TelAgentPhoneAgent
    from app.tel_agent_runtime import TelAgentTranscriptTurn
    activity = SpeechActivity(decoding=caller_arrives)
    events, spoken, processed = [], [], []
    class Connection:
        closed = False
        trace = None
        dropped_frames = 0
        close_reason = 'assistant_termination'
        speech_activity = activity
        async def start(self): return 'noise-recovery'
        def event(self, name, **_): events.append(name)
        async def close(self): self.closed = True
    connection = Connection()
    session = SimpleNamespace(state=IntakeState(call_uuid='noise-recovery'), call_id=1,
                              introduction_pending=False, interrupted_prompt_stage='issue', interrupted_from_stage='')
    class Core:
        settings = SimpleNamespace(local_voice_stack='streaming_trial', barge_in_resume_ms=20)
        def create_session(self, *_): return session
        async def call_started(self, *_): pass
        def greeting(self): return 'Thanks for calling Floodman.'
        def record_assistant(self, *_args, **_kwargs): pass
        async def no_input(self, *_): raise AssertionError('Noise must not trigger a twelve-second silence prompt')
        async def process(self, _session, text, **_):
            processed.append(text)
            return VoiceReply(text='Take your time.')
        async def disconnect(self, *_): pass
    monkeypatch.setattr('app.tel_agent_phone.AudioSocketConnection', lambda *_: connection)
    registry = SimpleNamespace(read_pre=lambda *_: {}, write_action=lambda *_: None)
    phone = TelAgentPhoneAgent(Core(), SimpleNamespace(streaming=True), None, None, registry)
    async def pump(_connection, queue, _session):
        if caller_arrives:
            await asyncio.sleep(.09)
            activity.decoding = False
            await queue.put(TelAgentTranscriptTurn(text='Please wait', heard_audio=True))
        await asyncio.Event().wait()
    async def speak(_connection, stream, *_args, **_kwargs):
        text = ''.join([part async for part in stream])
        spoken.append(text)
        interrupted = len(spoken) == 1
        if not interrupted:
            connection.closed = True
        return SimpleNamespace(text=text, interrupted=interrupted, first_audio_ms=1)
    monkeypatch.setattr(phone, '_pump_transcripts', pump)
    monkeypatch.setattr(phone, '_speak', speak)
    await asyncio.wait_for(phone.handle(None, SimpleNamespace(get_extra_info=lambda *_: None)), timeout=1)
    if caller_arrives:
        assert spoken == ['Thanks for calling Floodman.', 'Take your time.']
        assert processed == ['Please wait'] and 'empty_interruption_recovery' not in events
    else:
        assert spoken == ['Thanks for calling Floodman.'] * 2
        assert 'empty_interruption_recovery' in events and not processed
        assert not session.introduction_pending and session.interrupted_prompt_stage == ''


@pytest.mark.asyncio
async def test_farewell_recovery_finalizes_only_after_uninterrupted_replay(monkeypatch):
    from app.models import IntakeState, VoiceReply
    from app.tel_agent_phone import TelAgentPhoneAgent
    from app.tel_agent_runtime import TelAgentTranscriptTurn
    actions, spoken, finalized = [], [], []
    class Connection:
        closed = False
        trace = None
        dropped_frames = 0
        close_reason = 'assistant_termination'
        speech_activity = SpeechActivity()
        async def start(self): return 'farewell-recovery'
        def event(self, *_args, **_kwargs): pass
        async def close(self): self.closed = True
    call = Connection()
    session = SimpleNamespace(state=IntakeState(call_uuid='farewell-recovery', completed=True), call_id=1)
    class Core:
        settings = SimpleNamespace(local_voice_stack='streaming_trial', barge_in_resume_ms=20)
        def create_session(self, *_): return session
        async def call_started(self, *_): pass
        def greeting(self): return 'Hello.'
        def record_assistant(self, *_args, **_kwargs): pass
        async def process(self, *_args, **_kwargs): return VoiceReply(text='Goodbye.', end_call=True)
        async def finalize_completion(self, *_): finalized.append(True)
        async def disconnect(self, *_): pass
    monkeypatch.setattr('app.tel_agent_phone.AudioSocketConnection', lambda *_: call)
    phone = TelAgentPhoneAgent(Core(), SimpleNamespace(streaming=True), None, None,
                              SimpleNamespace(read_pre=lambda *_: {}, write_action=lambda *args: actions.append(args)))
    async def pump(_connection, queue, _session):
        await queue.put(TelAgentTranscriptTurn(text='No thanks', heard_audio=True))
        await asyncio.Event().wait()
    async def speak(_connection, stream, *_args, **_kwargs):
        assert not finalized
        text = ''.join([part async for part in stream])
        spoken.append(text)
        return SimpleNamespace(text=text, interrupted=len(spoken) == 2, first_audio_ms=1)
    monkeypatch.setattr(phone, '_pump_transcripts', pump)
    monkeypatch.setattr(phone, '_speak', speak)
    await asyncio.wait_for(phone.handle(None, SimpleNamespace(get_extra_info=lambda *_: None)), timeout=1)
    assert spoken == ['Hello.', 'Goodbye.', 'Goodbye.']
    assert finalized == [True] and actions == [('farewell-recovery', 'completed', '', 'completed')]


@pytest.mark.asyncio
async def test_audio_reader_filters_playback_echo_before_interruption_detection():
    import struct
    from app.audiosocket import TYPE_AUDIO, TYPE_HANGUP
    call = connection()
    call._echo_filter = PlaybackEchoFilter()
    call.output_active = True
    rng = np.random.default_rng(5)
    reference = rng.normal(0, 1800, 16000).astype('<i2')
    call._echo_filter.record(reference.tobytes())
    task = asyncio.create_task(call._reader_loop())
    for offset in range(3000, 11000, 160):
        data = (.6 * reference[offset:offset + 160]).astype('<i2').tobytes()
        call.reader.feed_data(bytes([TYPE_AUDIO]) + struct.pack('!H', len(data)) + data)
        await asyncio.sleep(0)
    call.reader.feed_data(bytes([TYPE_HANGUP, 0, 0]))
    await task
    assert not call.barge_in_event.is_set()
