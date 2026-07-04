"""Route decision (SPEC §5–§7).

ROLE: turn a final transcript into either a phrase command (executed
locally, never dispatched) or a RouteDecision. Degraded mode (no first mate):
phrase hit or forward-verbatim to the active session — full stop (user
decision, SPEC §14.9).

INVARIANTS: the first-mate tier is consulted through a port with a hard
timeout; any failure, timeout, or malformed output coerces to plain
`forward` (SPEC §7.3) — but a timeout no longer CANCELS the mate: dispatch
goes with the fast path and the mate finishes in the background (triage
2026-07-03: the mate must never slow the action; its late decision still
speaks/acts/corrects). The router never rewrites transcript text.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass

from voco.core import phrases
from voco.core.first_mate import FirstMatePort, fallback_target
from voco.core.phrases import PhraseCommand
from voco.core.turn import RouteDecision

FIRST_MATE_TIMEOUT_S = 0.8


@dataclass
class Routed:
    phrase: PhraseCommand | None = None
    decision: RouteDecision | None = None
    # Mate missed the deadline but keeps running; on_late will fire.
    late_pending: bool = False


class Router:
    def __init__(
        self,
        first_mate: FirstMatePort | None = None,
        timeout_s: float = FIRST_MATE_TIMEOUT_S,
    ) -> None:
        self._mate = first_mate
        self._timeout_s = timeout_s

    def set_timeout(self, seconds: float) -> None:
        """Runtime tuning knob (config.set first_mate.timeout_ms)."""
        if seconds <= 0:
            raise ValueError("timeout must be > 0")
        self._timeout_s = seconds

    async def decide(
        self,
        text: str,
        names: list[str],
        grounding: dict,
        speech_sink: Callable[[str], None] | None = None,
        on_late: Callable[[RouteDecision | None], None] | None = None,
    ) -> Routed:
        cmd = phrases.match(text, names)
        if cmd is not None:
            return Routed(phrase=cmd)
        if self._mate is None:
            return Routed(decision=RouteDecision(kind="forward"))
        # Streaming is capability-sniffed, not part of FirstMatePort: a
        # sink without a streaming mate silently uses the plain call.
        stream_fn = getattr(self._mate, "route_stream", None)
        if speech_sink is not None and stream_fn is not None:
            call = stream_fn(text, grounding, speech_sink)
        else:
            call = self._mate.route(text, grounding)
        task = asyncio.ensure_future(call)
        late_pending = False
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(task), timeout=self._timeout_s
            )
        except TimeoutError:
            decision = None
            if on_late is not None:
                # Deadline missed ≠ wasted work: the mate keeps running
                # (the adapter's own budget bounds it) and reports late.
                task.add_done_callback(lambda t: self._finish_late(t, on_late))
                late_pending = True
            else:
                task.cancel()
        except Exception:
            decision = None  # the task itself failed; nothing to continue
        if decision is None:
            # Mate missed its deadline: forward, but never to the WRONG
            # session — a spoken 'tell <name>' keeps its destination.
            return Routed(
                decision=RouteDecision(
                    kind="forward", target=fallback_target(text, names)
                ),
                late_pending=late_pending,
            )
        if decision.kind == "answer" and not decision.speech.strip():
            # Coercion rule: an empty local answer is a misroute (SPEC §7.3).
            decision = RouteDecision(kind="forward")
        return Routed(decision=decision)

    @staticmethod
    def _finish_late(
        task: asyncio.Task, on_late: Callable[[RouteDecision | None], None]
    ) -> None:
        decision: RouteDecision | None = None
        with contextlib.suppress(Exception):
            decision = task.result()
        with contextlib.suppress(Exception):
            on_late(decision)  # fail-silent: late extras must never raise
