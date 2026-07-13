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
import time
from collections.abc import Callable
from dataclasses import dataclass

SESSION_PREFIX = "voco-"

# How long a spawned command gets to prove it can start at all. Long
# enough for exec + a startup crash, short enough not to stall control.
STARTUP_GRACE_S = 0.8


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


def _default_runner(argv: list[str]) -> RunResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        # A missing binary is an environment fact, not a crash: surface
        # it through the same error path as any failed invocation.
        return RunResult(127, "", f"{argv[0]}: not installed (or not on PATH)")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", name).strip("-").lower()
    return slug or "session"


class TmuxManager:
    def __init__(
        self,
        runner: Runner = _default_runner,
        voco_url: str = "",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._run = runner
        self._voco_url = voco_url
        self._sleep = sleep

    def _tmux(self, args: list[str], host: str | None) -> RunResult:
        argv = ["tmux", *args]
        if host:
            # hosts can arrive from agent-supplied identity: a value shaped
            # like an option (-oProxyCommand=…) must never reach ssh's
            # argument parser (same dash-reject gate as diffsource)
            if host.startswith("-"):
                raise RuntimeError(f"invalid ssh host {host!r}")
            # ssh -T: no pty needed; tmux server runs detached on the host.
            # `--` ends option parsing so the host is always a hostname.
            argv = ["ssh", "-T", "--", host, *argv]
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
        """Start `harness_cmd` in a detached tmux session; returns its name.

        Verified: a command that dies at startup (bad flag, not on PATH)
        leaves new-session returning 0 and no session behind — a false
        success (live-test bug). remain-on-exit keeps the corpse so the
        failure is reported WITH the command's output, then cleaned up.
        """
        tmux_name = SESSION_PREFIX + slugify(name)
        args = ["new-session", "-d", "-s", tmux_name]
        if cwd:
            args += ["-c", cwd]
        if self._voco_url:
            args += ["-e", f"VOCO_URL={self._voco_url}"]
        args.append(harness_cmd)
        self._tmux(args, host)
        try:
            self._tmux(["set-option", "-t", tmux_name, "remain-on-exit", "on"], host)
            self._sleep(STARTUP_GRACE_S)
            status = self._tmux(
                [
                    "list-panes",
                    "-t",
                    tmux_name,
                    "-F",
                    "#{pane_dead} #{pane_dead_status}",
                ],
                host,
            ).stdout.strip()
        except RuntimeError as e:
            # Session already gone: it died before we could even pin it.
            raise RuntimeError(f"{harness_cmd!r} died at spawn: {e}") from e
        if status.startswith("1"):
            code = [*status.split(), "?"][1]
            try:
                lines = self.capture_pane(tmux_name, host=host).strip().splitlines()
                tail = " | ".join(ln for ln in lines[-3:] if ln.strip())
            except RuntimeError:
                tail = ""
            try:
                self._tmux(["kill-session", "-t", tmux_name], host)
            except RuntimeError:
                pass  # corpse cleanup is best-effort; the error below is the point
            raise RuntimeError(
                f"{harness_cmd!r} exited (status {code}) right after spawn:"
                f" {tail or 'no output'}"
            )
        # Healthy: back to normal lifecycle (a later exit closes the pane).
        self._tmux(["set-option", "-t", tmux_name, "-u", "remain-on-exit"], host)
        return tmux_name

    def kill(self, tmux_name: str, host: str | None = None) -> None:
        if not tmux_name.startswith(SESSION_PREFIX):
            raise ValueError(f"refusing to kill non-voco session {tmux_name!r}")
        self._tmux(["kill-session", "-t", tmux_name], host)

    def send_text(self, target: str, text: str, host: str | None = None) -> None:
        """Type literal text + Enter into a pane/session composer."""
        self._tmux(["send-keys", "-t", target, "-l", text], host)
        self._tmux(["send-keys", "-t", target, "Enter"], host)

    def send_escape(self, target: str, host: str | None = None) -> None:
        self._tmux(["send-keys", "-t", target, "Escape"], host)

    def capture_pane(self, target: str, host: str | None = None) -> str:
        return self._tmux(["capture-pane", "-p", "-t", target], host).stdout

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
