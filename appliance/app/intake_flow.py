from __future__ import annotations

from app.intake import spoken_address, spoken_email, spoken_phone
from app.models import IntakeState

CONTACT_ORDER = ("name", "email", "phone", "address")


def collection_question(state: IntakeState) -> str:
    stage = state.stage
    if stage == "issue":
        return "How can I help?"
    if stage == "property_context":
        return "Is this a home or a business?"
    if stage == "affected_area":
        return "Where on the property is the problem, and which rooms or materials are affected?"
    if stage == "timing_summary":
        if state.service_key == "mold_remediation":
            return "When did you first notice the mold or musty conditions?"
        if state.service_key == "water_damage_restoration":
            return "Is water actively coming in now, or when did this start?"
        if state.service_key == "foundation_repair":
            return "When did you first notice the foundation issue?"
        if state.service_key == "sump_pump_and_drainage":
            return "When did the pump or drainage problem start?"
        return "When did this start?"
    if stage == "safety_summary":
        return "Any electrical, sewage, or other safety concerns?"
    if stage == "source_summary":
        return "What do you think caused it, and how far has it spread?"
    if stage == "name":
        return "What name should I put this under?"
    if stage == "email":
        return "What's the best email for you? You can say skip."
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
        return f"I have {spoken_phone(value)} as your callback number", "Is that correct?"
    if field == "address":
        return f"I heard {spoken_address(value)}", "Is that correct?"
    return "", ""


def next_stage_after_confirmation(field: str) -> str:
    return {"name": "email", "email": "phone", "phone": "address", "address": "complete"}[field]


def contact_endpoint_stage(stage: str) -> bool:
    # Collection fields may contain spelling or natural pauses. Confirmation
    # answers are normally one-word yes/no responses and should endpoint on the
    # faster conversational timing path.
    return stage in {"name", "email", "phone", "address"}
