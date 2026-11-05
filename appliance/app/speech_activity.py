"""Per-call recognition activity used to recover from empty interruptions."""
from dataclasses import dataclass
import time


@dataclass(slots=True)
class SpeechActivity:
    last_voice_at: float = 0.0
    decoding: bool = False
    reset_requested: bool = False
    background_energy: float = 0.0

    def voiced(self) -> None:
        self.last_voice_at = time.perf_counter()

    def busy(self, grace_seconds: float = 1.8) -> bool:
        return self.decoding or time.perf_counter() - self.last_voice_at < grace_seconds
