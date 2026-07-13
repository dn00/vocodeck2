"""TerminalBackend port (SPEC-WORKBENCH §5, W4).

ROLE: the role-named seam over managed-session terminals. Two live
implementations, chosen per spawn: `tmux` (adapters/tmux.py — survives
daemon restarts, native attach, read-only workbench mirror) and `pty`
(adapters/ptyterm.py — live streamed xterm page, dies with the daemon
in v1, stated honestly).

The capability CELLS — not the backend name — drive every consumer
(SPEC principle: capability cells over platform switches): the client
picks stream-vs-mirror per cell, the watcher just calls capture().
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TerminalCells:
    """What this session's terminal can do (rides registry snapshot)."""

    backend: str  # "tmux" | "pty"
    stream: bool  # live byte stream (/v1/term WS)
    capture: bool  # point-in-time text capture
    send_keys: bool  # inject input
    resize: bool
    survives_restart: bool
    native_attach: bool  # `tmux attach` exists; pty has no native surface

    def to_dict(self) -> dict:
        return asdict(self)


TMUX_CELLS = TerminalCells(
    backend="tmux",
    stream=False,
    capture=True,
    send_keys=True,
    resize=False,
    survives_restart=True,
    native_attach=True,
)

PTY_CELLS = TerminalCells(
    backend="pty",
    stream=True,
    capture=True,
    send_keys=True,
    resize=True,
    survives_restart=False,
    native_attach=False,
)
