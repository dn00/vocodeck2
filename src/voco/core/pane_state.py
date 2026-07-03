"""Terminal-state heuristics for peeked panes (SPEC §9.2 companion).

ROLE: classify a captured pane (tmux capture-pane text) into a coarse
agent state — "waiting" (blocked on an approval/choice), "working", or
"shell" (no harness running) — from output patterns alone: no hooks, no
per-harness instrumentation (derive-don't-ask; approach validated by
herdr's zero-config state detection — concepts only, it is AGPL).

INVARIANTS: conservative — returns None when unsure, because a wrong
"waiting" spoken aloud is worse than silence; pure text in, verdict out
(adapters own the capture); waiting outranks working outranks shell,
since a permission prompt can coexist with spinner residue above it.
"""

from __future__ import annotations

import re

# Blocked on a human decision: numbered choice menus (Claude Code style),
# y/n confirms, and explicit ask-phrases from common harnesses.
_WAITING = [
    re.compile(r"❯\s*1\.", re.MULTILINE),
    re.compile(r"^\s*1\.\s.+\n\s*(?:❯\s*)?2\.\s", re.MULTILINE),
    re.compile(r"\[y/n\]|\(y/n\)|\by/N\b|\bY/n\b", re.IGNORECASE),
    re.compile(
        r"do you want to|would you like to|allow this|proceed\?|"
        r"press enter to continue|waiting for your (?:approval|input)",
        re.IGNORECASE,
    ),
]

# Actively generating/executing: interrupt hints and spinner glyphs.
_WORKING = [
    re.compile(r"esc to interrupt|ctrl\+c to interrupt", re.IGNORECASE),
    re.compile(r"[⠁-⣿]"),  # braille spinner range
    re.compile(r"[✻✽✶✳✢·]\s+\w+…"),  # "✻ Churning…" style status lines
]

# The harness exited: the last non-empty line looks like a bare shell
# prompt (short, ends in a prompt sigil, no sentence text after it).
_SHELL_PROMPT = re.compile(r"[$%>❯#]\s*$")

TAIL_LINES = 20  # only the visible tail matters; old scrollback lies


def classify(pane_text: str) -> str | None:
    """Coarse state of a captured pane, or None when unsure."""
    lines = [ln.rstrip() for ln in pane_text.splitlines() if ln.strip()]
    if not lines:
        return None
    tail = "\n".join(lines[-TAIL_LINES:])
    for pattern in _WAITING:
        if pattern.search(tail):
            return "waiting"
    for pattern in _WORKING:
        if pattern.search(tail):
            return "working"
    last = lines[-1]
    if len(last) <= 80 and _SHELL_PROMPT.search(last):
        return "shell"
    return None
