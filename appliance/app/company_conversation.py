"""Per-call context for company questions, separate from business authority."""
from __future__ import annotations

import re
from app.intake import classify_service, normalized


def question_words(text: str) -> bool:
    words = normalized(text)
    return bool(re.search(r"\b(?:what does .+ involve|how does .+ work)\b", words) or re.match(
        r"^(?:what|when|where|who|why|which|how|do you|does|(?:can|could|would|will|should|are) (?:you|i|we|it|that|this|they|there)|is (?:it|that|this|there|a|an|the|your|my|floodman)|tell me|explain|i was wondering|i m wondering|i am wondering|i m curious|i am curious)\b", words))


def is_question(text: str, *, has_topic: bool = False) -> bool:
    words = normalized(text)
    return bool("?" in text or question_words(text) or (has_topic and re.match(r"^(?:and |but )?(?:what about|how about|does that|would that|can that|is that|will that)\b", words)))


def search_query(text: str, dialogue: dict, description: str = "") -> str:
    topic = classify_service(text).get("service_key", "")
    if topic:
        return text
    if re.search(r"\b(?:it|that|this|those|they|process|also|too)\b", normalized(text)):
        previous = dialogue.get("turns", [])[-1:]
        return " ".join([text, dialogue.get("topic", ""), *(turn["question"] for turn in previous), description[:400]])
    return text


def needs_detail(text: str) -> bool:
    return bool(re.match(r"^(?:how|why|explain|tell me|what happens|what does|what is|what s|what about|and what about)\b", normalized(text)) or re.search(r"\b(?:compare|difference|versus)\b", normalized(text)))


def policy_question(text: str) -> bool:
    return bool(re.search(r"\b(?:cost|price|pricing|free|charge|charges|estimate|warranty|guarantee|insurance|deductible|billing|coverage|hours|open|emergency|serve|service area|come to)\b", normalized(text)))


def supportive_opening(text: str) -> str:
    words = normalized(text)
    if re.search(r"\b(?:not worried|not scared|not stressed|don t worry)\b", words):
        return ""
    if re.search(r"\b(?:i m|i am|we re|we are|i feel) (?:really |very |so |a little )?(?:worried|scared|stressed|overwhelmed|frustrated|upset)\b", words) or "i don t know what to do" in words:
        return "Let's take this one step at a time. "
    return ""


def context_summary(dialogue: dict, description: str) -> str:
    # Last question plus topic resolves references without resending an entire
    # generated answer on the CPU prompt path. Full history remains in memory.
    turns = dialogue.get("turns", [])[-1:]
    concern = f"Concern: {description[:120]}. " if description else ""
    return (concern + f"Topic: {dialogue.get('topic', '')}. " + " ".join(
        f"Previous question: {turn['question'][:150]}" for turn in turns
    ))[:400]


def remember(dialogue: dict, question: str, answer: str, topic: str = "") -> None:
    if topic:
        dialogue["topic"] = topic
    dialogue["turns"] = [*dialogue.get("turns", []), {"question": question[:400], "answer": answer[:900]}][-4:]


def safe_answer(text: str) -> bool:
    words = normalized(text)
    return bool(text.strip() and len(text) < 1000 and not re.search(
        r"\b(?:i ve|i have|we ve|we have|i just|we just) (?:booked|scheduled|transferred|charged|verified|cancelled)|"
        r"\b(?:i can|we can|i will|we will|let me) (?:book|schedule|transfer|verify|charge|cancel)\b|"
        r"\b(?:your appointment is|you are verified|your account is verified|guaranteed arrival|will arrive in|will be there in)\b", words
    ) and not re.search(r"\$\s*\w|\bdollars\b", text, re.I))
