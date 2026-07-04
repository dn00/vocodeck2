"""VAD hysteresis + capture buffer logic (SPEC §4.1) — fake prob model."""

from __future__ import annotations

import numpy as np

from voco.core.capture import CaptureBuffer, pre_roll_frames_for
from voco.core.vad import FRAME_MS, FRAME_SAMPLES, VadConfig, VadGate


def frame(value: int = 1000) -> np.ndarray:
    return np.full(FRAME_SAMPLES, value, dtype=np.int16)


class Script:
    """Feed a scripted prob sequence to the gate."""

    def __init__(self, reopenable: bool = False) -> None:
        self.events: list[str] = []
        self.probs: list[float] = []
        self._i = 0
        self.gate = VadGate(
            VadConfig(),
            model=self._model,
            on_speech_started=lambda: self.events.append("start"),
            on_speech_ended=lambda: self.events.append("end"),
            reopenable=lambda: reopenable,
        )

    def _model(self, f: np.ndarray) -> float:
        p = self.probs[self._i]
        self._i += 1
        return p

    def run(self, probs: list[float]) -> None:
        self.probs.extend(probs)
        for _ in probs:
            self.gate.process(frame())


def frames_for(ms: int) -> int:
    return ms // FRAME_MS


def test_entry_requires_min_speech_and_end_requires_min_silence():
    s = Script()
    # 320ms speech < 384ms entry: no event.
    s.run([0.9] * frames_for(320))
    assert s.events == []
    # Cross the entry bar.
    s.run([0.9] * frames_for(96))
    assert s.events == ["start"]
    # 32ms silence < 64ms: still in speech.
    s.run([0.1])
    assert s.events == ["start"]
    # Cross min_silence.
    s.run([0.1] * 2)
    assert s.events == ["start", "end"]


def test_continuation_threshold_applies_when_reopenable():
    s = Script(reopenable=True)
    # 192ms suffices while the turn is reopenable.
    s.run([0.9] * frames_for(192))
    assert s.events == ["start"]


def test_suppress_blocks_events_half_duplex():
    s = Script()
    s.gate.suppress(True)
    s.run([0.9] * frames_for(500))
    assert s.events == []
    s.gate.suppress(False)
    s.run([0.9] * frames_for(384))
    assert s.events == ["start"]


def test_suppress_mid_speech_closes_the_segment():
    """Duplex hot-switch while the user is speaking (echo rescue): the
    gate must close the open segment, not swallow speech_ended (stranding
    the machine in CAPTURING) or emit it stale after unsuppress."""
    s = Script()
    s.run([0.9] * frames_for(384))
    assert s.events == ["start"]
    s.gate.suppress(True)
    assert s.events == ["start", "end"]
    assert not s.gate.in_speech
    # Nothing stale fires once the gate reopens onto silence.
    s.gate.suppress(False)
    s.run([0.1] * 4)
    assert s.events == ["start", "end"]


def test_entry_run_tolerates_sub_gap_dips():
    """A single dipped frame must not restart the 384ms accumulation —
    that pushed speech_started far past the pre-roll (clipping bug)."""
    s = Script()
    s.run([0.9] * frames_for(320))  # under the entry bar
    s.run([0.1])  # 32ms dip < min_silence_ms(64): run survives
    s.run([0.9] * frames_for(64))  # 320+64 = 384: fires
    assert s.events == ["start"]
    # A real gap (>= min_silence) still resets the run.
    s2 = Script()
    s2.run([0.9] * frames_for(320))
    s2.run([0.1, 0.1])  # 64ms: reset
    s2.run([0.9] * frames_for(320))
    assert s2.events == []


def test_pre_roll_covers_the_entry_run():
    """Pre-roll must hold at least min_speech_ms + margin: everything the
    VAD saw before speech_started fires is utterance audio."""
    assert pre_roll_frames_for(384) * FRAME_MS >= 384 + 320
    assert pre_roll_frames_for(96) * FRAME_MS >= 96 + 320


def test_capture_buffer_preroll_and_merge():
    buf = CaptureBuffer(pre_roll_frames=10)
    for i in range(15):
        buf.feed(frame(i))
    buf.start_utterance()  # seeds with last 10 pre-roll frames
    buf.feed(frame(100))
    buf.pause()
    # Silence between segments is preserved by feeding through the pause?
    # No: paused frames go to pre-roll, not the utterance (gap dropped is
    # fine for STT; SPEC stitching preserves within-segment audio).
    buf.resume_utterance()
    buf.feed(frame(200))
    pcm = np.frombuffer(buf.take(), dtype=np.int16)
    assert len(pcm) == 12 * FRAME_SAMPLES  # 10 preroll + 2 speech frames
    assert pcm[-1] == 200
    buf.clear()
    assert buf.take() == b""
