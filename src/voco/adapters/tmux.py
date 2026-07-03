"""Managed sessions via tmux (SPEC §9.2) — the spawn/kill/list adapter.

ROLE: the daemon owns the pane, the user owns the terminal. Spawns a
harness inside a detached tmux session with the voco attach env pre-wired;
`--host` runs the same commands through ssh (remote tmux + the §9.1
tunnel). Impure edge: subprocess, injected for tests.

INVARIANTS: tmux session names are `voco-<slug>` so `list()` never touches
user tmux sessions; no native-Windows support (documented — WSL2/remote
only); failures raise with tmux's stderr so control surfaces them.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

SESSION_PREFIX = "voco-"


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


def _default_runner(argv: list[str]) -> RunResult:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", name).strip("-").lower()
    return slug or "session"


class TmuxManager:
    def __init__(self, runner: Runner = _default_runner, voco_url: str = "") -> None:
        self._run = runner
        self._voco_url = voco_url

    def _tmux(self, args: list[str], host: str | None) -> RunResult:
        argv = ["tmux", *args]
        if host:
            # ssh -T: no pty needed; tmux server runs detached on the host.
            argv = ["ssh", "-T", host, *argv]
        result = self._run(argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux failed ({' '.join(args[:2])}): {result.stderr.strip()}"
            )
        return result

    def spawn(
        self,
        harness_cmd: str,
        name: str,
        cwd: str | None = None,
        host: str | None = None,
    ) -> str:
        """Start `harness_cmd` in a detached tmux session; returns its name."""
        tmux_name = SESSION_PREFIX + slugify(name)
        args = ["new-session", "-d", "-s", tmux_name]
        if cwd:
            args += ["-c", cwd]
        if self._voco_url:
            args += ["-e", f"VOCO_URL={self._voco_url}"]
        args.append(harness_cmd)
        self._tmux(args, host)
        return tmux_name

    def kill(self, tmux_name: str, host: str | None = None) -> None:
        if not tmux_name.startswith(SESSION_PREFIX):
            raise ValueError(f"refusing to kill non-voco session {tmux_name!r}")
        self._tmux(["kill-session", "-t", tmux_name], host)

    def list(self, host: str | None = None) -> list[str]:
        try:
            result = self._tmux(["list-sessions", "-F", "#{session_name}"], host)
        except RuntimeError:
            return []  # no tmux server running = no sessions
        return [
            line
            for line in result.stdout.splitlines()
            if line.startswith(SESSION_PREFIX)
        ]
