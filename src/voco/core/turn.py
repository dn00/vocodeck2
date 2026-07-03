"""Turn state machine (SPEC §5).

ROLE: the single owner of turn lifecycle — capture, speculative hold,
routing, dispatch-closes-turn, reopen/merge. Pure logic: no audio, no
network, no asyncio; time enters via an injected monotonic `now()` and
leaves via `next_deadline()` which the async shell awaits.

INVARIANTS:
- One union-typed state (IDLE|CAPTURING|HOLDING|ROUTING|REOPENABLE); every
  transition is a named method with guards at the top.
- Dispatch is a one-way door: after `dispatch_ready` fires, that turn key
  never merges again; later speech opens a new turn (SPEC §5.2).
- Pre-dispatch speech ALWAYS merges into the open turn (a turn that has not
  been dispatched is one utterance); `reopen_window_ms` bounds only the
  post-local-reply REOPENABLE state. (Build-time clarification of §5.2,
  recorded in BUILD.md.)
- Stale async results are rejected by revision: every merge bumps
  `revision`; `stt_final`/`route_decided` carrying an older revision are
  ignored (speech-to-speech PR-307 revision semantics).
- dispatch_time = max(hold expiry, route decision); PTT release skips the
  hold term only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class TurnState(StrEnum):
    IDLE = "idle"
    CAPTURING = "capturing"
    HOLDING = "holding"
    ROUTING = "routing"
    REOPENABLE = "reopenable"


RouteKind = Literal["forward", "answer", "ack_forward"]


@dataclass
class RouteDecision:
    kind: RouteKind
    speech: str = ""
    target: str | None = None  # call name for targeted forward (Gemma-tier)
    action: dict | None = None


@dataclass
class TurnConfig:
    dispatch_hold_ms: int = 800
    reopen_window_ms: int = 1200


@dataclass
class TurnEvents:
    """Listener port; the shell wires these to audio/STT/router/arbitration.

    All callbacks are synchronous and must not re-enter the machine.
    """

    capture_started: Callable[[int, bool], None]  # (key, reopened)
    capture_stopped: Callable[[int, int], None]  # (key, revision) -> finalize STT
    chirp_requested: Callable[[int], None]  # (key) full-duplex instant ack
    cancel_speculation: Callable[[int, int], None]  # (key, old_revision)
    route_requested: Callable[[int, int, str], None]  # (key, revision, text)
    dispatch_ready: Callable[[int, str, RouteDecision], None]  # closes turn
    local_reply_ready: Callable[[int, RouteDecision], None]
    turn_state_changed: Callable[[int, TurnState], None]


@dataclass
class _Turn:
    key: int
    revision: int = 0
    vad_closed_at: float | None = None
    hold_deadline: float | None = None
    reopen_deadline: float | None = None
    text: str | None = None
    decision: RouteDecision | None = None
    hold_satisfied: bool = False
    ptt_held: bool = False
    dispatched: bool = False


class TurnMachine:
    def __init__(
        self,
        events: TurnEvents,
        config: TurnConfig | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._events = events
        self._cfg = config or TurnConfig()
        self._now = now or (lambda: 0.0)
        self._state = TurnState.IDLE
        self._turn: _Turn | None = None
        self._next_key = 1

    # ---- introspection -------------------------------------------------

    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def current_key(self) -> int | None:
        return self._turn.key if self._turn else None

    def next_deadline(self) -> float | None:
        """Earliest time at which on_deadline() must be called, or None."""
        if self._turn is None:
            return None
        if self._state is TurnState.HOLDING:
            return self._turn.hold_deadline
        if self._state is TurnState.REOPENABLE:
            return self._turn.reopen_deadline
        return None

    # ---- speech inputs (from VAD wrapper / PTT) ------------------------

    def speech_started(self) -> None:
        if self._state is TurnState.IDLE:
            self._open_turn(reopened=False)
        elif self._state is TurnState.CAPTURING:
            return  # already capturing
        elif self._state in (TurnState.HOLDING, TurnState.ROUTING):
            self._merge()  # pre-dispatch speech always merges
        elif self._state is TurnState.REOPENABLE:
            assert self._turn is not None
            if self._now() <= (self._turn.reopen_deadline or 0.0):
                self._merge()
            else:
                # Deadline passed but on_deadline not yet called; close and
                # start fresh rather than merging outside the window.
                self._close_turn()
                self._open_turn(reopened=False)

    def speech_ended(self) -> None:
        if self._state is not TurnState.CAPTURING:
            return  # guard: silence only matters while capturing
        assert self._turn is not None
        if self._turn.ptt_held:
            return  # PTT holds the floor; VAD silence cannot close capture
        self._begin_hold()

    def ptt_pressed(self) -> None:
        if self._state is TurnState.IDLE:
            self._open_turn(reopened=False)
        elif self._state in (
            TurnState.HOLDING,
            TurnState.ROUTING,
            TurnState.REOPENABLE,
        ):
            self._merge()
        assert self._turn is not None
        self._turn.ptt_held = True

    def ptt_released(self) -> None:
        if self._turn is None or not self._turn.ptt_held:
            return
        self._turn.ptt_held = False
        if self._state is TurnState.CAPTURING:
            # Explicit end: skip the hold term entirely (SPEC §4.4/§5.2).
            now = self._now()
            self._turn.vad_closed_at = now
            self._turn.reopen_deadline = now + self._cfg.reopen_window_ms / 1000.0
            self._turn.hold_satisfied = True
            self._events.capture_stopped(self._turn.key, self._turn.revision)
            self._events.chirp_requested(self._turn.key)
            self._to(TurnState.ROUTING)
            self._maybe_dispatch()

    # ---- async results (from STT / router) -----------------------------

    def stt_final(self, key: int, revision: int, text: str) -> None:
        if not self._is_current(key, revision):
            return
        assert self._turn is not None
        if not text.strip():
            # Empty final: nothing to route; abandon the turn (SPEC §4.2).
            self._close_turn()
            return
        self._turn.text = text
        self._events.route_requested(key, revision, text)

    def route_decided(self, key: int, revision: int, decision: RouteDecision) -> None:
        if not self._is_current(key, revision):
            return
        assert self._turn is not None
        self._turn.decision = decision
        self._maybe_dispatch()

    # ---- deadlines ------------------------------------------------------

    def on_deadline(self) -> None:
        if self._turn is None:
            return
        now = self._now()
        if self._state is TurnState.HOLDING:
            dl = self._turn.hold_deadline
            if dl is not None and now >= dl:
                self._turn.hold_satisfied = True
                self._to(TurnState.ROUTING)
                self._maybe_dispatch()
        elif self._state is TurnState.REOPENABLE:
            dl = self._turn.reopen_deadline
            if dl is not None and now >= dl:
                self._close_turn()

    # ---- internals -------------------------------------------------------

    def _is_current(self, key: int, revision: int) -> bool:
        return (
            self._turn is not None
            and self._turn.key == key
            and self._turn.revision == revision
            and not self._turn.dispatched
        )

    def _open_turn(self, reopened: bool) -> None:
        self._turn = _Turn(key=self._next_key)
        self._next_key += 1
        self._to(TurnState.CAPTURING)
        self._events.capture_started(self._turn.key, reopened)

    def _merge(self) -> None:
        """Resumed speech pre-dispatch (or within REOPENABLE window)."""
        assert self._turn is not None
        old_rev = self._turn.revision
        self._turn.revision += 1
        self._turn.text = None
        self._turn.decision = None
        self._turn.hold_satisfied = False
        self._turn.vad_closed_at = None
        self._turn.hold_deadline = None
        self._turn.reopen_deadline = None
        self._events.cancel_speculation(self._turn.key, old_rev)
        self._to(TurnState.CAPTURING)
        self._events.capture_started(self._turn.key, True)

    def _begin_hold(self) -> None:
        assert self._turn is not None
        now = self._now()
        self._turn.vad_closed_at = now
        self._turn.hold_deadline = now + self._cfg.dispatch_hold_ms / 1000.0
        self._turn.reopen_deadline = now + self._cfg.reopen_window_ms / 1000.0
        # Speculation starts at VAD close: STT finalize + instant chirp.
        self._events.capture_stopped(self._turn.key, self._turn.revision)
        self._events.chirp_requested(self._turn.key)
        self._to(TurnState.HOLDING)

    def _maybe_dispatch(self) -> None:
        """dispatch_time = max(hold, route): fire when both are satisfied."""
        assert self._turn is not None
        t = self._turn
        if not t.hold_satisfied or t.decision is None or t.text is None:
            return
        if t.decision.kind == "answer":
            self._events.local_reply_ready(t.key, t.decision)
            self._to(TurnState.REOPENABLE)
            return
        t.dispatched = True  # one-way door
        self._events.dispatch_ready(t.key, t.text, t.decision)
        self._close_turn()

    def _close_turn(self) -> None:
        self._turn = None
        self._to(TurnState.IDLE)

    def _to(self, state: TurnState) -> None:
        if state is self._state:
            return
        self._state = state
        key = self._turn.key if self._turn else 0
        self._events.turn_state_changed(key, state)
