from __future__ import annotations

import re

GOOGLE_FORWARD_PATTERNS = [
    re.compile(r"\b(?:this|the) call (?:is|was) from google\b", re.I),
    re.compile(r"\bcall from google\b", re.I),
    re.compile(r"\bgoogle (?:may|will) record (?:this|the) call\b", re.I),
    re.compile(r"\bthis call may be recorded by google\b", re.I),
    re.compile(r"\bto help improve google(?:'s)? services\b", re.I),
    re.compile(r"\bconnected through google\b", re.I),
    re.compile(r"\bgoogle local services\b", re.I),
]

GOOGLE_AUTOMATED_PATTERNS = [
    re.compile(r"\bi(?: am|'m) an automated (?:assistant|system) (?:calling )?(?:from|on behalf of) google\b", re.I),
    re.compile(r"\bautomated call (?:from|on behalf of) google\b", re.I),
    re.compile(r"\bcalling (?:from|on behalf of) google (?:maps )?to (?:confirm|verify|ask|check)\b", re.I),
    re.compile(r"\bgoogle (?:maps|business) (?:is )?calling to (?:confirm|verify|check)\b", re.I),
    re.compile(r"\bcalling about (?:your )?(?:business hours|services|availability|appointment|booking)\b", re.I),
    re.compile(r"\bcan your business (?:provide|perform|offer|schedule)\b", re.I),
]

SUSPICIOUS_GOOGLE_PATTERNS = [
    re.compile(r"\b(?:pay|payment|credit card|debit card|bank account)\b.{0,35}\bgoogle\b", re.I),
    re.compile(r"\bgoogle\b.{0,45}\b(?:pay|payment|credit card|debit card|bank account)\b", re.I),
    re.compile(r"\b(?:password|verification code|one[- ]time code|security code|login code)\b", re.I),
    re.compile(r"\b(?:remote access|remote into|install (?:an )?app|download software)\b", re.I),
    re.compile(r"\b(?:listing|profile) (?:will be|is being) (?:suspended|removed|deleted|closed)\b", re.I),
    re.compile(r"\bguarantee(?:d)? (?:first page|top|number one|ranking)\b", re.I),
    re.compile(r"\bpress (?:one|1) to (?:verify|claim|keep|activate)\b", re.I),
    re.compile(r"\brenew (?:your )?google (?:listing|profile)\b", re.I),
]

DIRECT_HUMAN_MARKERS = [
    re.compile(r"\b(?:i|we|my|our)\b", re.I),
    re.compile(r"\b(?:water|flood|basement|crawl ?space|foundation|mold|sump|drain|estimate|appointment|invoice|bill)\b", re.I),
    re.compile(r"\b(?:hello|hi|yes|good morning|good afternoon)\b", re.I),
    re.compile(r"\b(?:can you|could you|need help|looking for|calling about|want to|would like)\b", re.I),
]

KNOWN_ANNOUNCEMENT_FRAGMENTS = [
    "this call is from google",
    "the call is from google",
    "call from google",
    "google may record this call",
    "google will record this call",
    "this call may be recorded by google",
    "to help improve google services",
    "connected through google",
    "google local services",
]
