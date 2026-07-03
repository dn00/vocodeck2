"""openWakeWord adapter (SPEC §4.5) — the wake detector behind AttentionGate.

ROLE: turn 32ms mic frames into a wake-word score. openWakeWord natively
consumes 80ms (1280-sample) chunks, so this adapter buffers frames and
returns the latest score in between (scores are sticky for sub-chunk
frames — the gate thresholds, we don't).

INVARIANTS: lazy import (optional extra `wake`); the returned callable has
the same shape as a VAD model (frame → float) so VoiceLoopDeps.wake_loader
stays symmetric with load_vad_model; tuning (bare "voco" vs "hey voco") is
a measurement task at live-audio time, not code.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

CHUNK_SAMPLES = 1280  # openWakeWord's native 80ms @ 16kHz


def load_openwakeword(model_path: str) -> Callable[[np.ndarray], float]:
    from openwakeword.model import Model

    model = Model(wakeword_models=[model_path])
    buffer = np.zeros(0, dtype=np.int16)
    last_score = 0.0

    def score(frame: np.ndarray) -> float:
        nonlocal buffer, last_score
        buffer = np.concatenate([buffer, frame.astype(np.int16)])
        while len(buffer) >= CHUNK_SAMPLES:
            chunk, buffer = buffer[:CHUNK_SAMPLES], buffer[CHUNK_SAMPLES:]
            predictions = model.predict(chunk)
            last_score = max(predictions.values()) if predictions else 0.0
        return last_score

    return score
