"""Playback arbitration (SPEC §5.4).

ROLE: the single prioritized TTS queue. Decides what may start, what
preempts, what drops — rules 0–5. Pure logic over an injected Player port;
no audio imports.

INVARIANTS:
- Rule 0: nothing starts while the turn machine is CAPTURING/HOLDING,
  except cached earcons ≤400ms in full_duplex.
- Rule 1: barge-in flushes everything (playing + queued).
- Rule 2: an agent say for the CURRENT dispatched turn preempts local
  (first-mate/ack) speech mid-item; says for older turns queue.
- Rule 3: first-mate speech for a turn never plays after an agent say for
  that turn has played.
- Rule 4: fillers (acks) are droppable — discarded when real speech exists.
- Rule 5: background chimes never preempt anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Source(StrEnum):
    ACK = "ack"  # cached PCM earcons/fillers
    FIRST_MATE = "first_mate"  # the local voice tier (SPEC §7)
    AGENT = "agent"
    CHIME = "chime"


class DuplexMode(StrEnum):
    FULL = "full_duplex"
    HALF = "half_duplex"


@dataclass
class PlaybackItem:
    source: Source
    content: object  # opaque to arbitration; the player knows how to play it
    turn_id: str | None = None
    duration_ms: int | None = None  # known for cached items only


class Player(Protocol):
    def play(self, item: PlaybackItem) -> None: ...
    def stop(self) -> None: ...


_PRIORITY = {Source.AGENT: 0, Source.FIRST_MATE: 1, Source.ACK: 2, Source.CHIME: 3}

ACK_GATE_EXEMPT_MS = 400


class PlaybackQueue:
    def __init__(
        self,
        player: Player,
        emit: Callable[[str, dict], object] | None = None,
    ) -> None:
        self._player = player
        self._emit = emit or (lambda t, p: None)
        self._queue: list[PlaybackItem] = []
        self._playing: PlaybackItem | None = None
        self._gated = False  # rule 0: turn machine in CAPTURING/HOLDING
        self._duplex = DuplexMode.FULL
        self._current_turn: str | None = None  # last dispatched turn_id
        self._agent_spoke_turns: set[str] = set()  # rule 3

    # ---- context from the daemon ----------------------------------------

    def set_duplex(self, mode: DuplexMode) -> None:
        self._duplex = mode

    def set_gate(self, gated: bool) -> None:
        """Rule 0 gate; called on turn-state changes."""
        self._gated = gated
        if not gated:
            self._pump()

    def note_dispatch(self, turn_id: str) -> None:
        self._current_turn = turn_id

    # ---- inputs ----------------------------------------------------------

    def barge_in(self) -> None:
        """Rule 1: user speech/PTT flushes everything."""
        self._queue.clear()
        if self._playing is not None:
            self._interrupt("barge-in")

    def enqueue(self, item: PlaybackItem) -> None:
        # Rule 3: first-mate speech dead once the agent spoke for that turn.
        if (
            item.source is Source.FIRST_MATE
            and item.turn_id is not None
            and item.turn_id in self._agent_spoke_turns
        ):
            return
        # Rule 4: fillers are pointless when real speech exists.
        if item.source is Source.ACK and self._has_real_speech():
            return
        if item.source in (Source.FIRST_MATE, Source.AGENT):
            self._queue = [q for q in self._queue if q.source is not Source.ACK]
        # Rule 2: current-turn agent say preempts playing local speech.
        if (
            item.source is Source.AGENT
            and self._is_current_turn(item)
            and self._playing is not None
            and self._playing.source in (Source.FIRST_MATE, Source.ACK)
        ):
            self._interrupt("preempted")
        self._queue.append(item)
        self._queue.sort(key=lambda i: _PRIORITY[i.source])
        self._pump()

    def on_item_finished(self) -> None:
        item = self._playing
        self._playing = None
        if item is not None:
            if item.source is Source.AGENT and item.turn_id is not None:
                self._agent_spoke_turns.add(item.turn_id)
            self._emit(
                "speech.finished",
                {"source": item.source.value, "turn_id": item.turn_id},
            )
        self._pump()

    # ---- internals -------------------------------------------------------

    def _is_current_turn(self, item: PlaybackItem) -> bool:
        return item.turn_id is None or item.turn_id == self._current_turn

    def _has_real_speech(self) -> bool:
        real = (Source.FIRST_MATE, Source.AGENT)
        if self._playing is not None and self._playing.source in real:
            return True
        return any(q.source in real for q in self._queue)

    def _may_start(self, item: PlaybackItem) -> bool:
        if not self._gated:
            return True
        # Rule 0 exception: short cached earcons in full duplex only.
        return (
            item.source is Source.ACK
            and self._duplex is DuplexMode.FULL
            and item.duration_ms is not None
            and item.duration_ms <= ACK_GATE_EXEMPT_MS
        )

    def _interrupt(self, reason: str) -> None:
        item = self._playing
        self._playing = None
        self._player.stop()
        if item is not None:
            if item.source is Source.AGENT and item.turn_id is not None:
                self._agent_spoke_turns.add(item.turn_id)
            self._emit(
                "speech.interrupted",
                {
                    "source": item.source.value,
                    "turn_id": item.turn_id,
                    "reason": reason,
                },
            )

    def _pump(self) -> None:
        if self._playing is not None:
            return
        for i, item in enumerate(self._queue):
            if self._may_start(item):
                self._queue.pop(i)
                self._playing = item
                self._emit(
                    "speech.started",
                    {"source": item.source.value, "turn_id": item.turn_id},
                )
                self._player.play(item)
                return
