from app.intake_flow import collection_question, confirmation_question, contact_endpoint_stage
from app.models import IntakeState

def test_short_prompts():
    state = IntakeState(call_uuid="x", name="Josh Aldrich", email="josh@example.com", phone="+12318840943", address="1 Main Street")
    assert collection_question(state) == "How can I help?"
    assert confirmation_question(state, "name") == "Josh Aldrich, right?"
    assert "recorded" not in confirmation_question(state, "email").lower()
    assert contact_endpoint_stage("address")
    assert not contact_endpoint_stage("confirm_name")
    assert not contact_endpoint_stage("confirm_email")


def test_timing_question_uses_known_service_context():
    state = IntakeState(call_uuid="mold", stage="timing_summary", service_key="mold_remediation")
    assert collection_question(state) == "When did you first notice the mold or musty conditions?"
    state.service_key = "water_damage_restoration"
    assert "actively coming in" in collection_question(state)
