from __future__ import annotations

import asyncio

import pytest

from app.call_gate.audio_socket import UUID_FRAME, encode_frame, read_frame
from app.call_gate.classifier import CallGateClassifier
from app.models import CallType, GateClassificationRequest, GateState


def test_direct_customer_routes_immediately(settings):
    decision = CallGateClassifier(settings).classify(
        GateClassificationRequest(
            transcript="Hi, I have water coming into my basement.", elapsed_seconds=2.0
        )
    )
    assert decision.call_type == CallType.DIRECT_CUSTOMER
    assert decision.ready is True
    assert decision.opening_transcript.startswith("Hi")


def test_google_forward_announcement_waits_then_preserves_customer_turn(settings):
    classifier = CallGateClassifier(settings)
    waiting = classifier.classify(
        GateClassificationRequest(transcript="This call may be recorded by Google. Call from Google.", elapsed_seconds=3)
    )
    assert waiting.state == GateState.WAITING_FOR_HUMAN
    assert waiting.ready is False
    ready = classifier.classify(
        GateClassificationRequest(
            transcript="This call may be recorded by Google. Call from Google. Hi, my basement is flooding.",
            source_hint="google_lsa",
            elapsed_seconds=6,
        )
    )
    assert ready.call_type == CallType.GOOGLE_FORWARDED_CUSTOMER
    assert ready.ready is True
    assert "basement is flooding" in ready.opening_transcript
    assert "call from google" not in ready.opening_transcript


def test_google_automated_business_and_scam_are_separate(settings):
    classifier = CallGateClassifier(settings)
    automated = classifier.classify(
        GateClassificationRequest(
            transcript="I am an automated assistant calling from Google to confirm your business hours.",
            elapsed_seconds=3,
        )
    )
    assert automated.call_type == CallType.GOOGLE_AUTOMATED_BUSINESS
    assert automated.agent == settings.google_business_agent
    scam = classifier.classify(
        GateClassificationRequest(
            transcript="Your Google listing will be removed. Give me the verification code now.",
            elapsed_seconds=3,
        )
    )
    assert scam.call_type == CallType.SUSPICIOUS_GOOGLE
    assert scam.state == GateState.SECURITY_BLOCK


def test_gate_timeout_fails_open(settings):
    decision = CallGateClassifier(settings).classify(
        GateClassificationRequest(transcript="", elapsed_seconds=20, timed_out=True)
    )
    assert decision.ready is True
    assert decision.state == GateState.TIMEOUT
    assert decision.agent == settings.default_agent
    assert decision.metadata["fail_open"] is True


@pytest.mark.asyncio
async def test_audiosocket_frame_is_big_endian_and_round_trips():
    payload = b"1234567890abcdef"
    frame = encode_frame(UUID_FRAME, payload)
    assert frame[:3] == bytes([UUID_FRAME, 0, 16])
    reader = asyncio.StreamReader()
    reader.feed_data(frame)
    reader.feed_eof()
    frame_type, decoded = await read_frame(reader)
    assert frame_type == UUID_FRAME
    assert decoded == payload


def test_normal_customer_calling_about_appointment_is_not_google_automation(settings):
    decision = CallGateClassifier(settings).classify(
        GateClassificationRequest(
            transcript="Hi, I'm calling about my appointment for the basement inspection.",
            elapsed_seconds=2.0,
        )
    )
    assert decision.call_type == CallType.DIRECT_CUSTOMER
    assert decision.agent == settings.default_agent


def test_forwarded_customer_generic_help_request_is_preserved(settings):
    decision = CallGateClassifier(settings).classify(
        GateClassificationRequest(
            transcript="Call from Google. Hi, can you help me with a problem?",
            source_hint="google_lsa",
            elapsed_seconds=5,
        )
    )
    assert decision.call_type == CallType.GOOGLE_FORWARDED_CUSTOMER
    assert decision.ready is True
    assert "can you help me" in decision.opening_transcript


def test_no_speech_gate_timeout_is_shorter_than_full_google_window(settings):
    assert settings.gate_no_speech_timeout_seconds < settings.gate_max_seconds
