from __future__ import annotations

from dataclasses import dataclass, field

from app.call_gate.classifier import CallGateClassifier
from app.models import GateClassificationRequest, GateDecision, GateState


@dataclass(slots=True)
class GateStateMachine:
    classifier: CallGateClassifier
    transcript: str = ""
    state: GateState = GateState.REGISTERED
    history: list[GateState] = field(default_factory=lambda: [GateState.REGISTERED])

    def feed(
        self,
        transcript: str,
        *,
        source_hint: str = "",
        did: str = "",
        caller_number: str = "",
        elapsed_seconds: float = 0.0,
        timed_out: bool = False,
    ) -> GateDecision:
        if transcript.strip():
            self.transcript = transcript.strip()
        decision = self.classifier.classify(
            GateClassificationRequest(
                transcript=self.transcript,
                source_hint=source_hint,
                did=did,
                caller_number=caller_number,
                elapsed_seconds=elapsed_seconds,
                timed_out=timed_out,
            )
        )
        self.state = decision.state
        if not self.history or self.history[-1] != self.state:
            self.history.append(self.state)
        return decision
