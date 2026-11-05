"""Conservative wording validation; routing and field selection remain in code."""
from __future__ import annotations

import re


CONCEPTS = {
    "water_condition": (r"\bwater\b", r"\b(?:coming|flowing|rising|stopped|stop|active|entering)\b"),
    "affected_area": (r"\b(?:where|area|part|room|location)\b",),
    "timing_summary": (r"\b(?:when|long|start|noticed|notice|happening)\b",),
    "source_summary": (r"\b(?:source|cause|caused|coming from|where.*from|leak)\b",),
    "service_detail": (r"\b(?:help|assist|need|pass along|message)\b",),
}


def valid_question_rewrite(candidate: str, required: str, stage: str) -> bool:
    if len(candidate) > 260 or candidate.count("?") != 1 or not candidate.endswith("?"):
        return False
    if any(character in candidate for character in ("\n", "*", "#", "`")):
        return False
    # A voice rewrite cannot introduce numbers or claim a protected action.
    if set(re.findall(r"\d+", candidate)) - set(re.findall(r"\d+", required)):
        return False
    forbidden = r"\b(?:verif\w*|consent|approv\w*|dispatch\w*|schedul\w*|transfer\w*|connect\w*|emergency|hazard\w*|sewage|electrical|guarantee\w*|insurance|price|cost|payment|free|book\w*)\b"
    if re.search(forbidden, candidate, re.I):
        return False
    if re.search(r"\b(?:will|i'll|we'll|i have|i've|we have|we've)\b", candidate, re.I):
        return False
    return all(re.search(concept, candidate, re.I) for concept in CONCEPTS.get(stage, (r"(?!)",)))
