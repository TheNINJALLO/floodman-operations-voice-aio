from __future__ import annotations

from app.intake import spoken_email, spoken_phone
from app.models import IntakeState

CONTACT_ORDER = ("name", "email", "phone", "address")


def collection_question(state: IntakeState) -> str:
    stage = state.stage
    if stage == "issue":
        return "How can I help?"
    if stage == "property_context":
        return "Is this a home or a business?"
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
    value = getattr(state, field)
    if field == "name":
        return f"{value}, right?"
    if field == "email":
        return "No email, right?" if state.email_status in {"declined", "unavailable"} else f"{spoken_email(value)}, right?"
    if field == "phone":
        if state.caller_number and state.phone == state.caller_number:
            return "Is this the best number to call you back on?"
        return f"{spoken_phone(value)}, right?"
    if field == "address":
        return f"{value}, right?"
    return ""


def next_stage_after_confirmation(field: str) -> str:
    return {"name": "email", "email": "phone", "phone": "address", "address": "complete"}[field]


def contact_endpoint_stage(stage: str) -> bool:
    # Collection fields may contain spelling or natural pauses. Confirmation
    # answers are normally one-word yes/no responses and should endpoint on the
    # faster conversational timing path.
    return stage in {"name", "email", "phone", "address"}
