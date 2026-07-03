"""Route decision (SPEC §5–§7).

ROLE: turn a final transcript into either a phrase command (executed
locally, never dispatched) or a RouteDecision. Degraded mode (no Gemma):
phrase hit or forward-verbatim to the active session — full stop (user
decision, SPEC §14.9).

INVARIANTS: the Gemma tier is consulted through a port with a hard timeout;
any failure, timeout, or malformed output coerces to plain `forward`
(SPEC §7.3). The router never rewrites transcript text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from voco.core import phrases
from voco.core.phrases import PhraseCommand
from voco.core.turn import RouteDecision

GEMMA_TIMEOUT_S = 0.8


class LlmTier(Protocol):
    """M1: the Gemma contract adapter (SPEC §7). Returns None to decline."""

    async def route(self, text: str, grounding: dict) -> RouteDecision | None: ...


@dataclass
class Routed:
    phrase: PhraseCommand | None = None
    decision: RouteDecision | None = None


class Router:
    def __init__(self, llm: LlmTier | None = None) -> None:
        self._llm = llm

    async def decide(self, text: str, names: list[str], grounding: dict) -> Routed:
        cmd = phrases.match(text, names)
        if cmd is not None:
            return Routed(phrase=cmd)
        if self._llm is None:
            return Routed(decision=RouteDecision(kind="forward"))
        try:
            decision = await asyncio.wait_for(
                self._llm.route(text, grounding), timeout=GEMMA_TIMEOUT_S
            )
        except Exception:
            decision = None
        if decision is None:
            return Routed(decision=RouteDecision(kind="forward"))
        if decision.kind == "answer" and not decision.speech.strip():
            # Coercion rule: an empty local answer is a misroute (SPEC §7.3).
            decision = RouteDecision(kind="forward")
        return Routed(decision=decision)
