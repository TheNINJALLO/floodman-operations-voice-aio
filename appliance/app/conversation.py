"""Fast, source-grounded conversation planning; no model or business actions."""
from __future__ import annotations

import re
from typing import Any

from app.intake import clean, classify_property_context, normalize_email, normalize_name, normalize_phone, normalized
from app.models import IntakeState

DETAIL_STAGES = {"affected_area", "timing_summary", "source_summary", "safety_summary", "water_condition", "service_detail"}
CONTACT_FIELDS = ("name", "phone", "address", "email")
LIGHT_KINDS = {"warranty", "quote_followup", "message"}
QUESTIONS = {
    "water_condition": "Is water still coming in or rising, or has it stopped?",
    "service_detail": "What would you like the team to help with?",
    "anything_else": "Is there anything else you'd like me to pass along?",
    "additional_message": "What else would you like me to include?",
}


def unfinished_turn(transcript: str) -> bool:
    """Wait for speech fragments instead of treating them as field answers."""
    text = normalized(transcript)
    if not text:
        return True
    if transcript.rstrip().endswith(("...", "…", "--")):
        return True
    if text in {"um", "uh", "erm", "hmm", "oh", "well", "so", "let me think"}:
        return True
    # Streaming recognition can append a hesitation to a short opening while
    # the caller is still forming the rest of the sentence.
    if re.fullmatch(r"(?:it s|it has|its) been(?: (?:at first|well|um|uh|like|so))*", text):
        return True
    return bool(re.search(r"\b(?:it s been|it has been|there s|there is|i was|we were|and|because|but|my name is|my email is|my address is)$", text))


def memory(state: IntakeState) -> dict[str, Any]:
    return state.metadata.setdefault("conversation", {"version": 1, "kind": "service", "asked": [], "declined": []})


def request_kind(transcript: str) -> str:
    text = normalized(transcript)
    if any(term in text for term in ("warranty documentation", "copy of the warranty", "warranty in writing", "written warranty", "warranty paperwork")):
        return "warranty"
    if any(term in text for term in ("quote number", "previous quote", "prior quote", "existing quote", "following up on a quote", "follow up on my quote")):
        return "quote_followup"
    if any(term in text for term in ("leave a message", "take a message", "general liability audit", "job application", "employment", "vendor", "question about my bill", "copy of my invoice", "reschedule", "cancel my appointment", "you came out", "you came to our property", "haven t heard back", "haven t heard from")):
        return "message"
    return "service"


def declined(transcript: str) -> bool:
    text = normalized(transcript)
    return text in {"skip", "no", "nope", "no thanks", "rather not", "no email", "none", "i don t know", "i do not know", "not sure"} or any(
        phrase in text for phrase in ("don t want to give", "do not want to give", "prefer not to", "rather not give", "don t have", "do not have")
    )


def hold_requested(transcript: str) -> bool:
    return normalized(transcript) in {
        "hold on", "hang on", "one moment", "just a moment", "give me a second", "give me a sec",
        "give me a minute", "just a second", "let me check", "let me look", "please wait",
    }


def identity_question(transcript: str) -> bool:
    return normalized(transcript) in {
        "are you a real person", "are you human", "is this a computer", "am i speaking with a computer",
        "are you an ai", "are you a robot", "are you a person", "is this an ai",
    }


def _clauses(transcript: str) -> list[str]:
    return [clean(part, 500) for part in re.split(r"[;!?]|\.\s+|,\s*(?:and\s+)?(?=my (?:name|email|address|phone)|call me)", transcript) if clean(part)]


def explicit_name(transcript: str) -> str:
    match = re.search(r"\b(?:my name is|this is|I am|I'm)\s+([A-Za-z][A-Za-z' -]{0,79}?)(?=[,.!?;]|\s+(?:and|calling|from|with)\b|$)", transcript, re.I)
    if not match or normalized(match[1]).split()[0] in {
        "a", "an", "the", "not", "just", "looking", "having", "calling", "wondering", "trying", "interested", "hoping", "sorry", "water", "mold", "about", "at", "in", "for", "my", "our", "concerned",
    } or declined(match[1]):
        return ""
    return normalize_name(match[1])


def name_answer(transcript: str) -> str:
    value = explicit_name(transcript)
    if value:
        return value
    value = re.split(r"[,;]|\.\s+(?=(?:my|call|I live)\b)|\s+and\s+(?=(?:my (?:email|phone|address|number)|call me)\b)", transcript, maxsplit=1, flags=re.I)[0]
    return normalize_name(value)


def phone_answer(transcript: str) -> str:
    introduced = re.search(r"\b(?:call me (?:back )?(?:at|on)|my (?:callback|phone|mobile|cell)(?: number)? is)\s*([+\d(][\d ().-]{6,30})", transcript, re.I)
    if introduced:
        return normalize_phone(introduced[1])
    leading = re.match(r"\s*([+\d(][\d ().-]{6,30})", transcript)
    return normalize_phone(leading[1]) if leading else normalize_phone(transcript)


def capture_volunteered(state: IntakeState, transcript: str, *, expected: str = "") -> None:
    """Copy clear caller phrases, never inferred property types or contact values."""
    info = memory(state)
    clauses = _clauses(transcript)
    for clause in clauses:
        if re.match(r"^(?:my name is|this is|I am|I'm)\b", clause, re.I) and (name := explicit_name(clause)):
            clause = clause[clause.find(name) + len(name):].lstrip(" ,.")
        text = normalized(clause)
        if "@" in clause or re.match(r"^(?:my (?:email|phone|callback|address)|call me|i live at)\b", text):
            continue
        property_context = classify_property_context(clause)
        # The recognition repair for the one-word answer 'hope' belongs only
        # to the explicit property question, not to open-ended conversation.
        property_word = re.search(r"\b(home|house|residential|homeowner|business|commercial|office|store|rental property|property manager)\b", text)
        if property_context and property_word and not state.property_context:
            state.property_context = property_context
        if not state.property_context and re.search(r"\b(museum|restaurant|warehouse|school|church|hotel)\b", text):
            state.property_context = "Commercial or managed property"
        if not state.affected_area and re.search(r"\b(basement|crawl ?space|bathroom|bedroom|kitchen|hallway|garage|foundation|ceiling|walls?|floors?)\b", text):
            state.affected_area = clause
        if not state.timing_summary and re.search(r"\b(today|yesterday|tonight|this morning|last (?:night|week|month|year)|\w+ (?:days?|weeks?|months?|years?) ago|for (?:a|an|\w+|a few|a couple of) (?:days?|weeks?|months?|years?)|since|whenever|every time|every (?:spring|summer|fall|winter)|when it rains|only when|right now|still leaking|has stopped|water is off|(?:on |started |noticed |began )(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", text):
            state.timing_summary = clause
        if not state.source_summary and re.search(r"\b(rain|rains|snowmelt|snow melt|pipe|supply line|discharge line|drain tile|storm|sump pump)\b", text):
            state.source_summary = clause
        if not state.safety_summary and re.search(r"\b(no safety concerns|no electrical (?:issues|concerns|hazards)|no sewage|all safe|nothing unsafe|no immediate danger)\b", text):
            state.safety_summary = clause
        if re.search(r"\b(standing water|just (?:the )?humidity|mainly damp|only damp|wet spots|damp walls|no water|no flooding|just moisture|currently dry|dry now|completely dry)\b", text):
            info["water_condition"] = clause

    # Contact values need an explicit introduction or a literal email. A phone
    # number embedded in an address/quote is never treated as a callback number.
    if expected != "confirm_name" and not state.name:
        state.name = explicit_name(transcript)
    if expected not in {"phone", "confirm_phone"}:
        match = re.search(r"\b(?:call me (?:back )?(?:at|on)|my (?:callback|phone|mobile|cell)(?: number)? is)\s*([+\d(][\d ().-]{6,30})", transcript, re.I)
        if match and (number := normalize_phone(match[1])) and number != state.phone:
            state.phone = number
            state.confirmations.pop("phone", None)
    if expected not in {"email", "confirm_email"}:
        match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", transcript)
        if not match:
            match = re.search(r"\bmy email(?: address)? is\s+(.+?)(?=[;!?]|,\s+and\b|$)", transcript, re.I)
            candidate = normalize_email(match[1]) if match else ""
        else:
            candidate = normalize_email(match[0])
        if candidate and candidate != state.email:
            state.email, state.email_status = candidate, "provided"
            state.confirmations.pop("email", None)
    if expected not in {"address", "confirm_address"} and not state.address:
        match = re.search(r"\b(?:my address is|the service address is|the property is at|I live at)\s+(\d.+?)(?=[;!?]|,?\s+and my\b|$)", transcript, re.I)
        if match:
            state.address = clean(match[1], 500).rstrip(".")


def detail_stage(state: IntakeState) -> str:
    info = memory(state)
    if info["kind"] in LIGHT_KINDS or state.service_status == "unsupported":
        return ""
    asked = info["asked"]
    water_related = state.service_key in {"water_damage_restoration", "basement_waterproofing", "crawl_space_encapsulation", "sump_pump_and_drainage"} or bool(re.search(r"\b(water|damp|humidity|moisture|leak)\b", normalized(state.description)))
    candidates = []
    if not state.service_key and not state.affected_area:
        candidates.append("service_detail")
    if not state.affected_area:
        candidates.append("affected_area")
    if water_related and not info.get("water_condition"):
        candidates.append("water_condition")
    if not state.timing_summary:
        candidates.append("timing_summary")
    # Ask only two useful follow-ups. Hazards volunteered by a caller still
    # reach the deterministic emergency handler; there is no hazard checklist.
    if len([stage for stage in asked if stage != "safety_summary"]) < 2:
        for candidate in candidates:
            if candidate not in asked:
                return candidate
    return ""


def contact_fields(state: IntakeState) -> tuple[str, ...]:
    kind = memory(state)["kind"]
    if kind in LIGHT_KINDS or state.service_status == "unsupported":
        # A documentation request can use email, but a general message does not
        # require an unrelated address or an email collection exercise.
        return ("name", "phone", "email") if kind == "warranty" or state.email else ("name", "phone")
    return CONTACT_FIELDS
