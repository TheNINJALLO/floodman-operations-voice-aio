"""CPU-only Silero inference with separate recurrent state for every caller."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np


@lru_cache(maxsize=2)
def load_silero_session(path: str):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])


class SileroGate:
    """Convert arbitrary 8 kHz PCM frames into 32 ms Silero windows.

    Only immutable model weights are shared. State, context and pending samples
    belong to this gate and must never be reused by another caller.
    """

    def __init__(self, path: Path, *, session=None):
        self.session = session if session is not None else load_silero_session(str(path.resolve()))
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 32), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.last_probability: float | None = None

    def probability(self, pcm16le: bytes) -> float | None:
        if not pcm16le:
            return self.last_probability
        samples = np.frombuffer(pcm16le, dtype="<i2").astype(np.float32) / 32768.0
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= 256:
            frame, self.pending = self.pending[:256], self.pending[256:]
            window = np.concatenate((self.context, frame[None, :]), axis=1)
            probability, self.state = self.session.run(None, {
                "input": window, "state": self.state, "sr": np.array(8000, dtype=np.int64),
            })
            self.context = frame[None, -32:].copy()
            self.last_probability = float(np.asarray(probability).reshape(-1)[0])
        return self.last_probability
