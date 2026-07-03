"""Silero VAD wrapper with duration hysteresis (SPEC §4.1).

ROLE: turn per-frame speech probabilities into clean semantic events for
the turn machine: `speech_started` once speech has run ≥ the entry (or
continuation) threshold, `speech_ended` once silence has run ≥
min_silence_ms. The onnx model is injected as a callable so hysteresis is
testable without onnxruntime.

INVARIANTS: frame size is fixed (512 samples @ 16kHz = 32ms — silero v5's
native window); the continuation threshold (192ms) applies only while the
shell reports the current turn as reopenable (SPEC §4.1); thresholds are
config, never hardwired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 512
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE  # 32ms


@dataclass
class VadConfig:
    threshold: float = 0.5
    min_speech_ms: int = 384
    min_speech_continuation_ms: int = 192
    min_silence_ms: int = 64


class VadGate:
    def __init__(
        self,
        config: VadConfig,
        model: Callable[[np.ndarray], float],
        on_speech_started: Callable[[], None],
        on_speech_ended: Callable[[], None],
        reopenable: Callable[[], bool] = lambda: False,
    ) -> None:
        self._cfg = config
        self._model = model
        self._started = on_speech_started
        self._ended = on_speech_ended
        self._reopenable = reopenable
        self._in_speech = False
        self._speech_run_ms = 0
        self._silence_run_ms = 0
        self._suppressed = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def suppress(self, suppressed: bool) -> None:
        """Half-duplex: mute the gate during TTS playback + grace (§4.4)."""
        self._suppressed = suppressed
        if suppressed:
            self._speech_run_ms = 0

    def process(self, frame: np.ndarray) -> None:
        if self._suppressed:
            return
        prob = self._model(frame)
        if prob >= self._cfg.threshold:
            self._speech_run_ms += FRAME_MS
            self._silence_run_ms = 0
            if not self._in_speech and self._speech_run_ms >= self._entry_ms():
                self._in_speech = True
                self._started()
        else:
            self._silence_run_ms += FRAME_MS
            self._speech_run_ms = 0
            if self._in_speech and self._silence_run_ms >= self._cfg.min_silence_ms:
                self._in_speech = False
                self._ended()

    def _entry_ms(self) -> int:
        if self._reopenable():
            return self._cfg.min_speech_continuation_ms
        return self._cfg.min_speech_ms


def load_silero(model_path: str) -> Callable[[np.ndarray], float]:
    """Production model: silero-vad v5 onnx. Lazy import of onnxruntime."""
    import onnxruntime as ort  # noqa: PLC0415

    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def run(frame: np.ndarray) -> float:
        nonlocal state
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32) / 32768.0
        out, state = sess.run(
            None, {"input": frame.reshape(1, -1), "state": state, "sr": sr}
        )
        return float(out[0][0])

    return run
