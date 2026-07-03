"""Utterance capture buffer (SPEC §4.1) — pure logic.

ROLE: accumulate utterance audio (with pre-roll so the first
syllable isn't clipped). The turn machine's shell asks for the accumulated
utterance at finalize time; merges keep accumulating into the same buffer.

INVARIANTS: 16kHz mono int16 throughout; pre-roll ring holds ~320ms; the
utterance buffer grows only between capture_started and capture_stopped
(+ merge reopens). Hardware lives in adapters/microphone.py.
"""

from __future__ import annotations

import collections

import numpy as np

PRE_ROLL_FRAMES = 10  # ~320ms


class CaptureBuffer:
    """Pure buffer logic (testable); the stream feeds it frames."""

    def __init__(self) -> None:
        self._pre_roll: collections.deque[np.ndarray] = collections.deque(
            maxlen=PRE_ROLL_FRAMES
        )
        self._utterance: list[np.ndarray] = []
        self._recording = False

    def feed(self, frame: np.ndarray) -> None:
        if self._recording:
            self._utterance.append(frame)
        else:
            self._pre_roll.append(frame)

    def start_utterance(self) -> None:
        """capture_started(reopened=False): seed with pre-roll."""
        if not self._recording:
            self._utterance = list(self._pre_roll)
            self._recording = True

    def resume_utterance(self) -> None:
        """capture_started(reopened=True): keep prior audio, keep recording.

        Merged utterances preserve the silence gap (SPEC PR-307 stitching).
        """
        self._recording = True

    def pause(self) -> None:
        """VAD close: stop growing, keep contents for possible merge."""
        self._recording = False

    def take(self) -> bytes:
        """Finalize: the full utterance (across merges) as int16 PCM."""
        if not self._utterance:
            return b""
        return np.concatenate(self._utterance).astype(np.int16).tobytes()

    def clear(self) -> None:
        self._utterance = []
        self._recording = False
