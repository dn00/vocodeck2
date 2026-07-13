"""Shared fake audio edges for VoiceLoop-level tests.

One home for the fakes so daemon-level tests (config hot-apply, control
surface) can compose a REAL VoiceLoop exactly like test_voice_loop does.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class FakeTts:
    voice = "test"
    sample_rate = 24_000

    def __init__(self, **kwargs: Any) -> None:
        self.synthesized: list[tuple[str, str | None]] = []

    async def synth_bytes(self, text: str) -> bytes:
        return b"\x00\x00" * 240  # 10ms of silence

    def stream(self, text: str, voice: str | None = None):
        async def chunks():
            self.synthesized.append((text, voice))
            yield b"\x00\x00" * 240

        return chunks()


class FakeStt:
    def __init__(self, canned: str) -> None:
        self.canned = canned
        self.received: list[bytes] = []

    def transcribe(self, pcm: bytes) -> str:
        self.received.append(pcm)
        return self.canned if pcm else ""


class FakeMic:
    def __init__(self, on_frame, device=None, on_error=None) -> None:
        self.started = False
        self.on_error = on_error

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


class FakePlayer:
    """Records items; playback 'finishes' only when the test pumps it."""

    def __init__(self, on_finished, on_playing_changed, **kwargs: Any) -> None:
        self.on_finished = on_finished
        self.on_playing_changed = on_playing_changed
        self.items: list[Any] = []
        self.stops = 0

    def bind_loop(self, loop) -> None:
        pass

    def play(self, item) -> None:
        self.items.append(item)

    def stop(self) -> None:
        self.stops += 1

    def finish_current(self) -> None:
        self.on_finished()


class ScriptedVad:
    """frame value >= 500 counts as speech (prob 0.9), else silence."""

    def __call__(self, frame: np.ndarray) -> float:
        return 0.9 if int(frame[0]) >= 500 else 0.1
