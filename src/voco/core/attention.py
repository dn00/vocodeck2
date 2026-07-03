"""Attention modes — when is the turn machine armed at all? (SPEC §4.5)

ROLE: pure gate in front of the turn machine's speech inputs. Orthogonal
to duplex: duplex decides whether the mic hears during playback; attention
decides whether heard speech may open a turn.

INVARIANTS: `muted` blocks everything including PTT (the privacy switch);
`wake` arms a conversation window refreshed by each completed turn; wake
detection itself is an adapter (openWakeWord) — this gate only holds the
window state, so the logic is testable without any wake engine.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class AttentionMode(StrEnum):
    ALWAYS = "always"
    WAKE = "wake"
    PTT_ONLY = "ptt_only"
    MUTED = "muted"


DEFAULT_WAKE_WINDOW_S = 30.0


class AttentionGate:
    def __init__(
        self,
        mode: AttentionMode = AttentionMode.ALWAYS,
        now: Callable[[], float] = lambda: 0.0,
        wake_window_s: float = DEFAULT_WAKE_WINDOW_S,
    ) -> None:
        self.mode = mode
        self._now = now
        self._window_s = wake_window_s
        self._armed_until = float("-inf")

    def set_mode(self, mode: AttentionMode) -> None:
        self.mode = mode
        if mode is not AttentionMode.WAKE:
            self._armed_until = float("-inf")

    def allows_vad(self) -> bool:
        if self.mode is AttentionMode.ALWAYS:
            return True
        if self.mode is AttentionMode.WAKE:
            return self._now() <= self._armed_until
        return False

    def allows_ptt(self) -> bool:
        return self.mode is not AttentionMode.MUTED

    def on_wake_word(self) -> None:
        """Wake detector fired: arm the conversation window."""
        if self.mode is AttentionMode.WAKE:
            self._armed_until = self._now() + self._window_s

    def on_turn_activity(self) -> None:
        """A turn completed (dispatch/local reply): keep the window open."""
        if self.mode is AttentionMode.WAKE and self._now() <= self._armed_until:
            self._armed_until = self._now() + self._window_s
