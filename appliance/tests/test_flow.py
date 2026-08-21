from app.intake_flow import collection_question, confirmation_question, contact_endpoint_stage
from app.models import IntakeState

def test_short_prompts():
    state = IntakeState(call_uuid="x", name="Josh Aldrich", email="josh@example.com", phone="+12318840943", address="1 Main Street")
    assert collection_question(state) == "How can I help?"
    assert confirmation_question(state, "name") == "Josh Aldrich, right?"
    assert "recorded" not in confirmation_question(state, "email").lower()
    assert contact_endpoint_stage("address")
