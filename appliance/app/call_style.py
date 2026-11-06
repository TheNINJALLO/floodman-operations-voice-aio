"""Conversation wording adapted from the owner's OutreachGenius intake prompt.

Used by the immediate deterministic replies and the optional wording model.
Routing, verification and business authority remain in their existing code.
"""
import re

from app.intake import normalized

GREETING = "Hello, this is Avery, Floodman's digital assistant. How may I help you today?"
INTRODUCTION = "You're through to Floodman. I'm Avery, the digital assistant."
IDENTITY = "I'm Avery, Floodman's digital assistant. This is an automated conversation."
FAREWELL = "Thanks for calling Floodman, and take care. Goodbye."

STYLE_GUIDANCE = (
    "You are Avery, Floodman's digital assistant. Use the owner's OutreachGenius "
    "conversation style: calm, friendly and reassuring; concise without sounding clipped. "
    "Ask one question and wait. Acknowledge briefly without parroting the caller. "
    "Use contractions and polite requests. Avoid cheerful acknowledgments after bad news. "
    "Accept details out of order. Clarify the missing or incorrect detail, never guess it. "
    "Ask for a missing last name once. Spell the email local part clearly at normal pace, "
    "with a naturally spoken domain. Ask the final anything-else question once per call. "
    "When more information follows, acknowledge it and leave the floor open without "
    "repeating that question. The caller may finish or correct a farewell. Close warmly, "
    "without reciting the intake. Never claim to be a human."
)


def farewell_body(hours: int) -> str:
    return f"The team will be in touch within {hours} hours. {FAREWELL}"


def extra_acknowledgment(index: int, transcript: str = "") -> str:
    words = normalized(transcript)
    if re.search(r"\b(?:estimate|inspection|consultation)\b", words):
        answer = "Of course. I'll let the team know you'd like an inspection."
    elif match := re.search(r"\buse (?:the )?(front|side|back|rear) (door|entrance)\b", words):
        answer = f"Thanks for letting me know. I'll tell the team to use the {match[1]} {match[2]}."
    elif re.search(r"\b(?:call|phone|reach)\b.+\b(?:after|before|morning|afternoon|evening|lunch)\b", words):
        answer = "Thanks. I'll include your preferred callback time for the team."
    elif re.search(r"\b(?:worried|scared|stressed|overwhelmed|upset|nervous)\b", words):
        answer = "I'm sorry you're dealing with this. We'll take it one step at a time."
    else:
        choices = ("Thank you, that's helpful.", "Thanks for explaining that.", "I understand. Thank you for letting me know.")
        answer = choices[index % len(choices)]
    if transcript and index == 0:
        answer += " Take your time if you'd like to clarify anything."
    return answer
