"""Conservative playback-reference subtraction for private 8 kHz phone audio.

This removes a highly correlated playback component, retaining unrelated caller
speech. It is a small echo suppressor, not a replacement for handset acoustic AEC.
"""
from __future__ import annotations

import time
import numpy as np


class PlaybackEchoFilter:
    def __init__(self):
        self.reference = np.zeros(0, dtype=np.float32)
        self.incoming = np.zeros(0, dtype=np.float32)
        self.last_output_at = 0.0
        self.last_input_at = 0.0
        self.last_correlation = 0.0

    def record(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
        self.reference = np.concatenate((self.reference, samples))[-12800:]
        self.last_output_at = time.perf_counter()

    def filter(self, pcm: bytes) -> bytes:
        now = time.perf_counter()
        self.last_correlation = 0.0
        if now - self.last_output_at > 1.0:
            self.incoming = np.zeros(0, dtype=np.float32)
            return pcm
        if now - self.last_input_at > .15:
            self.incoming = np.zeros(0, dtype=np.float32)
        self.last_input_at = now
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
        output = []
        for offset in range(0, len(samples), 160):
            frame = samples[offset:offset + 160]
            self.incoming = np.concatenate((self.incoming, frame))[-640:]
            cleaned = frame
            if len(self.incoming) == 640 and len(self.reference) >= 640:
                reference = self.reference[-(len(self.reference) // 4) * 4:]
                target = self.incoming
                coarse_ref = reference.reshape(-1, 4).mean(axis=1)
                coarse_target = target.reshape(-1, 4).mean(axis=1)
                coarse_target = coarse_target - coarse_target.mean()
                power = float(np.dot(coarse_target, coarse_target))
                if power > 160 * 40 ** 2:
                    sums = np.concatenate(([0.0], np.cumsum(coarse_ref, dtype=np.float64)))
                    squares = np.concatenate(([0.0], np.cumsum(coarse_ref ** 2, dtype=np.float64)))
                    n = len(coarse_target)
                    energy = squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n
                    numerator = np.correlate(coarse_ref, coarse_target, mode='valid')
                    correlations = numerator / np.sqrt(np.maximum(energy * power, 1))
                    coarse = int(np.argmax(np.abs(correlations)))
                    if abs(float(correlations[coarse])) >= .55:
                        centered = target - target.mean()
                        target_power = float(np.dot(centered, centered))
                        best = None
                        for start in range(max(0, coarse * 4 - 4), min(len(reference) - 640, coarse * 4 + 4) + 1):
                            match = reference[start:start + 640]
                            match = match - match.mean()
                            match_power = float(np.dot(match, match))
                            dot = float(np.dot(centered, match))
                            correlation = dot / np.sqrt(max(1, match_power * target_power))
                            if best is None or abs(correlation) > abs(best[0]):
                                best = (correlation, dot / max(1, match_power), start)
                        if best is not None and abs(best[0]) >= .68 and abs(best[1]) <= 3:
                            match = reference[best[2]:best[2] + 640]
                            correlation, gain = best[:2]
                            nonlinear_minimum = max(.90, abs(correlation) + .01)
                            # Speakerphone limiting compresses peaks. A linear
                            # fit can leave voiced echo even at a correct lag.
                            # Only use a compressed reference when it explains
                            # the input substantially better with high confidence.
                            for limit in (800, 1600, 3200, 6400):
                                candidate = limit * np.tanh(reference[best[2]:best[2] + 640] / limit)
                                centered_candidate = candidate - candidate.mean()
                                candidate_power = float(np.dot(centered_candidate, centered_candidate))
                                candidate_dot = float(np.dot(centered, centered_candidate))
                                candidate_correlation = candidate_dot / np.sqrt(max(1, candidate_power * target_power))
                                candidate_gain = candidate_dot / max(1, candidate_power)
                                if abs(candidate_correlation) >= nonlinear_minimum and abs(candidate_correlation) > abs(correlation) and abs(candidate_gain) <= 3:
                                    match, correlation, gain = candidate, candidate_correlation, candidate_gain
                            cleaned = frame - gain * match[-len(frame):]
                            self.last_correlation = max(self.last_correlation, abs(correlation))
            output.append(cleaned)
        return np.clip(np.concatenate(output), -32768, 32767).astype('<i2').tobytes() if output else pcm
