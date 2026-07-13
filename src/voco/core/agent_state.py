"""Derived display state — the rail dot (SPEC-WORKBENCH §6).

ROLE: one pure function from harness-agnostic facts (bridge session
state, pane hint, idle age) to the single display state a UI renders.
Harness-specific knowledge never enters here — it lives in identity
derivation, pane-pattern data, and spawn templates only.

INVARIANTS: total precedence gone > blocked > working > listening >
stale > idle — every session maps to exactly one dot; a parked agent is
never `blocked` (bridge truth wins over pane heuristics; the hint may
lag or misfire on prompt-shaped output).
"""

from __future__ import annotations

from typing import Literal

DisplayState = Literal[
    "gone", "disconnected", "blocked", "working", "listening", "stale", "idle"
]


def display_state(
    *,
    bridge_state: str,  # parked | working | idle (SPEC §8.2)
    pane_hint: str | None = None,  # waiting | working | shell (confirmed)
    idle_for_s: float = 0.0,
    stale_after_s: float = 600.0,
    handle_alive: bool | None = None,  # managed-terminal handle; None = unmanaged
) -> DisplayState:
    if pane_hint == "shell" or handle_alive is False:
        return "gone"
    if pane_hint == "waiting" and bridge_state != "parked":
        return "blocked"
    if bridge_state == "working" or pane_hint == "working":
        return "working"
    if bridge_state == "parked":
        return "listening"
    if idle_for_s > stale_after_s:
        return "stale"
    return "idle"
