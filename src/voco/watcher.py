"""Background pane watcher — the deck's eyes on unattended terminals.

ROLE: periodically capture the pane behind every inject-capable,
non-parked session, classify it (core.pane_state), and record the hint
on the session (registry emits pane.hint on change). On a CONFIRMED
"waiting" edge — two consecutive sightings, because a spoken false alarm
is worse than a late one — invoke the daemon's on_waiting callback
(proactive voice: "Helena looks like she's waiting on you").

INVARIANTS: observation only — never injects, never dispatches; capture
runs in the executor (subprocess never blocks the loop); a capture
failure is a None hint, not an error event per tick (peeking is a named
best-effort contract — a dead pane already surfaces through the session
lifecycle); parked sessions are skipped (their agent is reporting truth
through the bridge already).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import TYPE_CHECKING

from voco.core.pane_state import classify

if TYPE_CHECKING:
    from voco.adapters.tmux import TmuxManager
    from voco.core.registry import Registry, Session

CONFIRM_SIGHTINGS = 2  # consecutive "waiting" polls before speaking


class PaneWatcher:
    def __init__(
        self,
        registry: Registry,
        tmux: TmuxManager,
        *,
        interval_s: float = 3.0,
        on_waiting: Callable[[Session], None] | None = None,
    ) -> None:
        self._registry = registry
        self._tmux = tmux
        self._interval = interval_s
        self._on_waiting = on_waiting
        self._waiting_streak: dict[str, int] = {}
        self._announced: set[str] = set()

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    async def poll_once(self) -> None:
        loop = asyncio.get_running_loop()
        for s in list(self._registry.all()):
            target = s.inject_target
            if target is None or s.parked:
                continue
            host = s.identity.get("host_alias")
            try:
                text = await loop.run_in_executor(
                    None, functools.partial(self._tmux.capture_pane, target, host=host)
                )
                hint = classify(text)
            except Exception:
                hint = None  # named fail-silent: observation is best-effort
            self._registry.set_pane_hint(s.session_id, hint)
            self._track_waiting(s, hint)

    def _track_waiting(self, s: Session, hint: str | None) -> None:
        sid = s.session_id
        if hint != "waiting":
            self._waiting_streak.pop(sid, None)
            self._announced.discard(sid)
            return
        streak = self._waiting_streak.get(sid, 0) + 1
        self._waiting_streak[sid] = streak
        if streak >= CONFIRM_SIGHTINGS and sid not in self._announced:
            self._announced.add(sid)  # once per waiting episode
            if self._on_waiting is not None:
                self._on_waiting(s)
