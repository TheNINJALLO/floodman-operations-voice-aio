from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import Settings
from app.models import CallType, GateClassificationRequest, GateDecision, GateState
from app.call_gate.patterns import (
    DIRECT_HUMAN_MARKERS,
    GOOGLE_AUTOMATED_PATTERNS,
    GOOGLE_FORWARD_PATTERNS,
    KNOWN_ANNOUNCEMENT_FRAGMENTS,
    SUSPICIOUS_GOOGLE_PATTERNS,
)


def normalize_text(value: str) -> str:
    value = value.lower().replace("’", "'")
    value = re.sub(r"[^a-z0-9+' ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _matches(patterns: list[re.Pattern[str]], text: str) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


def _word_count(text: str) -> int:
    return len(re.findall(r"[a-z0-9']+", text))


def strip_google_announcement(text: str) -> str:
    cleaned = normalize_text(text)
    for fragment in KNOWN_ANNOUNCEMENT_FRAGMENTS:
        cleaned = cleaned.replace(fragment, " ")
    cleaned = re.sub(
        r"\b(?:please wait|while we connect|you are now connected|thank you|this call|may be recorded|for quality purposes)\b",
        " ",
        cleaned,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass(slots=True)
class CallGateClassifier:
    settings: Settings

    def classify(self, request: GateClassificationRequest) -> GateDecision:
        text = normalize_text(request.transcript)
        source = normalize_text(request.source_hint)
        suspicious_hits = _matches(SUSPICIOUS_GOOGLE_PATTERNS, text)
        automated_hits = _matches(GOOGLE_AUTOMATED_PATTERNS, text)
        forward_hits = _matches(GOOGLE_FORWARD_PATTERNS, text)
        source_is_google = any(token in source for token in ("google", "lsa", "local services", "ads"))
        google_context = source_is_google or "google" in text
        explicit_automation = any(
            marker in text
            for marker in ("automated assistant", "automated system", "automated call")
        )

        if suspicious_hits and (google_context or "listing" in text or "remote access" in text):
            confidence = min(0.99, 0.80 + suspicious_hits * 0.06)
            return GateDecision(
                call_type=CallType.SUSPICIOUS_GOOGLE,
                state=GateState.SECURITY_BLOCK,
                confidence=confidence,
                agent=self.settings.suspicious_google_agent,
                provider=self.settings.default_provider,
                transcript=request.transcript,
                opening_transcript="",
                announcement_detected="google" in text,
                ready=True,
                reason="Sensitive credential, payment, pressure, or remote-access language detected.",
                metadata={"suspicious_hits": suspicious_hits, "source_hint": request.source_hint},
            )

        if automated_hits and (google_context or explicit_automation):
            confidence = min(0.99, 0.84 + automated_hits * 0.05 + (0.04 if source_is_google else 0.0))
            return GateDecision(
                call_type=CallType.GOOGLE_AUTOMATED_BUSINESS,
                state=GateState.READY,
                confidence=confidence,
                agent=self.settings.google_business_agent,
                provider=self.settings.default_provider,
                transcript=request.transcript,
                opening_transcript=request.transcript,
                announcement_detected=True,
                ready=True,
                reason="Google automated business-information language detected.",
                metadata={"automated_hits": automated_hits, "source_hint": request.source_hint},
            )

        if forward_hits or (source_is_google and "record" in text):
            remainder = strip_google_announcement(text)
            human_markers = _matches(DIRECT_HUMAN_MARKERS, remainder)
            has_customer_turn = _word_count(remainder) >= 3 and human_markers > 0
            if has_customer_turn or request.timed_out:
                confidence = min(0.99, 0.79 + forward_hits * 0.05 + (0.06 if source_is_google else 0.0))
                return GateDecision(
                    call_type=CallType.GOOGLE_FORWARDED_CUSTOMER,
                    state=GateState.READY if has_customer_turn else GateState.TIMEOUT,
                    confidence=confidence,
                    agent=self.settings.google_forwarded_agent,
                    provider=self.settings.default_provider,
                    transcript=request.transcript,
                    opening_transcript=remainder if has_customer_turn else "",
                    announcement_detected=True,
                    ready=True,
                    reason=(
                        "Google forwarding announcement completed and live-customer speech followed."
                        if has_customer_turn
                        else "Google forwarding announcement detected; gate timed out safely into customer service."
                    ),
                    metadata={
                        "forward_hits": forward_hits,
                        "source_hint": request.source_hint,
                        "human_markers": human_markers,
                    },
                )
            return GateDecision(
                call_type=CallType.GOOGLE_FORWARDED_CUSTOMER,
                state=GateState.WAITING_FOR_HUMAN,
                confidence=min(0.95, 0.70 + forward_hits * 0.06 + (0.05 if source_is_google else 0.0)),
                agent=self.settings.google_forwarded_agent,
                provider=self.settings.default_provider,
                transcript=request.transcript,
                opening_transcript="",
                announcement_detected=True,
                ready=False,
                reason="Google forwarding announcement detected; waiting for the live caller.",
                metadata={"forward_hits": forward_hits, "source_hint": request.source_hint},
            )

        if text and request.elapsed_seconds >= self.settings.gate_min_seconds:
            human_markers = _matches(DIRECT_HUMAN_MARKERS, text)
            if _word_count(text) >= 2 or human_markers:
                return GateDecision(
                    call_type=CallType.DIRECT_CUSTOMER,
                    state=GateState.READY,
                    confidence=min(0.96, 0.72 + human_markers * 0.05),
                    agent=self.settings.default_agent,
                    provider=self.settings.default_provider,
                    transcript=request.transcript,
                    opening_transcript=request.transcript,
                    announcement_detected=False,
                    ready=True,
                    reason="Natural caller speech detected without a Google announcement.",
                    metadata={"human_markers": human_markers, "source_hint": request.source_hint},
                )

        if request.timed_out:
            call_type = CallType.GOOGLE_FORWARDED_CUSTOMER if source_is_google else CallType.UNKNOWN
            agent = self.settings.google_forwarded_agent if source_is_google else self.settings.default_agent
            return GateDecision(
                call_type=call_type,
                state=GateState.TIMEOUT,
                confidence=0.55 if source_is_google else 0.35,
                agent=agent,
                provider=self.settings.default_provider,
                transcript=request.transcript,
                opening_transcript=request.transcript,
                announcement_detected=False,
                ready=True,
                reason="No reliable classification before timeout; routed to ordinary customer service.",
                metadata={"source_hint": request.source_hint, "fail_open": True},
            )

        return GateDecision(
            call_type=CallType.UNKNOWN,
            state=GateState.LISTENING,
            confidence=0.1,
            agent=self.settings.default_agent,
            provider=self.settings.default_provider,
            transcript=request.transcript,
            opening_transcript="",
            announcement_detected=False,
            ready=False,
            reason="Collecting more opening audio.",
            metadata={"source_hint": request.source_hint},
        )
