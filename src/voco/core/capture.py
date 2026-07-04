"""Utterance capture buffer (SPEC §4.1) — pure logic.

ROLE: accumulate utterance audio (with pre-roll so the first
syllable isn't clipped). The turn machine's shell asks for the accumulated
utterance at finalize time; merges keep accumulating into the same buffer.

INVARIANTS: 16kHz mono int16 throughout; the pre-roll ring MUST cover the
VAD entry run (speech_started fires only after min_speech_ms of speech has
already passed) plus onset lag — an undersized ring clips the first words
of every utterance (live-test bug). The utterance buffer grows only
between capture_started and capture_stopped (+ merge reopens). Hardware
lives in adapters/microphone.py.
"""

from __future__ import annotations

import collections

import numpy as np

from voco.core.vad import FRAME_MS

# Beyond the entry run itself: silero's probability lags the true onset by
# a frame or two, and sub-min_silence dips during the run delay the trigger
# without resetting it. Both eat pre-roll.
PRE_ROLL_MARGIN_MS = 320


def pre_roll_frames_for(min_speech_ms: int) -> int:
    """Ring size that keeps the whole utterance once the VAD entry run
    (min_speech_ms) finally fires speech_started."""
    return -(-(min_speech_ms + PRE_ROLL_MARGIN_MS) // FRAME_MS)


class CaptureBuffer:
    """Pure buffer logic (testable); the stream feeds it frames."""

    def __init__(self, pre_roll_frames: int) -> None:
        self._pre_roll: collections.deque[np.ndarray] = collections.deque(
            maxlen=pre_roll_frames
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

    def drop_pre_roll(self) -> None:
        """Half-duplex playback just ended: the ring holds speaker-echo
        tail, not user speech — starting the next utterance with it would
        feed the bot's own words to STT."""
        self._pre_roll.clear()

    def clear(self) -> None:
        self._utterance = []
        self._recording = False
