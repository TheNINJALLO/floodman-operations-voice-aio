"""Notification sink for the text simulator; production delivery stays separate."""
from __future__ import annotations

from app.models import IntakeState


class SimulationNotifier:
    async def call_started(self, call_id: int, state: IntakeState) -> int:
        return 0

    async def send(self, call_id: int, state: IntakeState, *, kind: str, partial: bool) -> int:
        return 0
