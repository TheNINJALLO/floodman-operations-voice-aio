from __future__ import annotations

from app.intake import spoken_address, spoken_email, spoken_phone
from app.models import IntakeState
from app.conversation import QUESTIONS

CONTACT_ORDER = ("name", "phone", "address", "email")


def collection_question(state: IntakeState) -> str:
    stage = state.stage
    if stage in QUESTIONS:
        return QUESTIONS[stage]
    if stage == "issue":
        return "How can I help?"
    if stage == "property_context":
        return "Is this a home or a business?"
    if stage == "affected_area":
        return "Where are you noticing the problem?"
    if stage == "timing_summary":
        if state.service_key == "mold_remediation":
            return "When did you first notice the mold or musty conditions?"
        if state.service_key == "water_damage_restoration":
            return "When did you first notice the water?"
        if state.service_key == "foundation_repair":
            return "When did you first notice the foundation issue?"
        if state.service_key == "sump_pump_and_drainage":
            return "When did the pump or drainage problem start?"
        return "When did this start?"
    if stage == "safety_summary":
        return "Any electrical, sewage, or other safety concerns?"
    if stage == "source_summary":
        return "Do you know where it's coming from?"
    if stage == "name":
        return "What name should I put this under?"
    if stage == "email":
        return "What's the best email for you?"
    if stage == "phone":
        return "What's the best callback number?"
    if stage == "address":
        return "What's the full service address?"
    return ""


def confirmation_question(state: IntakeState, field: str) -> str:
    readback, question = confirmation_parts(state, field)
    if not readback:
        return question
    return f"{readback}. {question}"


def confirmation_parts(state: IntakeState, field: str) -> tuple[str, str]:
    value = getattr(state, field)
    if field == "name":
        return f"I heard {value}", "Is that correct?"
    if field == "email":
        if state.email_status in {"declined", "unavailable"}:
            return "I have no email address for you", "Is that correct?"
        return f"I heard {spoken_email(value)}", "Is that correct?"
    if field == "phone":
        if value and value == state.caller_number:
            return "", "Is this the best number to call you back on?"
        return f"I have {spoken_phone(value)} as your callback number", "Is that correct?"
    if field == "address":
        return f"I heard {spoken_address(value)}", "Is that correct?"
    return "", ""


def next_stage_after_confirmation(field: str) -> str:
    return {"name": "phone", "phone": "address", "address": "email", "email": "anything_else"}[field]


def contact_endpoint_stage(stage: str) -> bool:
    # Collection fields may contain spelling or natural pauses. Confirmation
    # answers are normally one-word yes/no responses and should endpoint on the
    # faster conversational timing path.
    return stage in {"name", "email", "phone", "address"}
