"""Silero VAD onnx runtime (SPEC §4.1) — adapter for core.vad.VadGate.

ROLE: load the silero-vad v5 model and expose it as the plain
frame→probability callable the VadGate expects. Impure edge: onnxruntime.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from voco.core.vad import SAMPLE_RATE

CONTEXT_SAMPLES = 64  # silero v5 requires the previous frame's tail prepended


def load_silero(model_path: str) -> Callable[[np.ndarray], float]:
    import onnxruntime as ort

    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
    sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def run(frame: np.ndarray) -> float:
        nonlocal state, context
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32) / 32768.0
        window = np.concatenate([context, frame])
        context = frame[-CONTEXT_SAMPLES:]
        out, state = sess.run(
            None, {"input": window.reshape(1, -1), "state": state, "sr": sr}
        )
        return float(out[0][0])

    return run
