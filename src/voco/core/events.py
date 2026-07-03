"""Event bus (SPEC §10).

ROLE: single fan-out point for protocol events — assigns the global
monotonic seq, stamps ts, delivers to subscribers (WS connections, logs).
Transport-free: subscribers are plain callables.

INVARIANTS: seq is per-daemon-run and gapless from the bus's view; emit
never raises into the caller (a broken subscriber is dropped and reported
once via daemon.error to the remaining subscribers).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from voco.protocol.messages import Envelope, make_event

Subscriber = Callable[[Envelope], None]


class EventBus:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._seq = 0
        self._subs: list[Subscriber] = []

    def subscribe(self, sub: Subscriber) -> Callable[[], None]:
        self._subs.append(sub)
        return lambda: self._subs.remove(sub) if sub in self._subs else None

    def make(self, type_: str, payload: dict[str, Any]) -> Envelope:
        """Stamp seq/ts WITHOUT fan-out (per-connection snapshot)."""
        self._seq += 1
        env = make_event(type_, payload)
        env.seq = self._seq
        env.ts = self._now()
        return env

    def emit(self, type_: str, payload: dict[str, Any]) -> Envelope:
        self._seq += 1
        env = make_event(type_, payload)
        env.seq = self._seq
        env.ts = self._now()
        dead: list[Subscriber] = []
        for sub in self._subs:
            try:
                sub(env)
            except Exception:
                dead.append(sub)
        for sub in dead:
            self._subs.remove(sub)
        if dead:
            self.emit("daemon.error", {"error": "subscriber dropped"})
        return env
