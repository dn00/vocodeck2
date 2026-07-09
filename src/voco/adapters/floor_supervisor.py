"""TTS floor supervision (BUILD-PROD P3): voco-d owns the floor
process so nobody hand-runs a stale `voco-tts-floor` for a week again.

The daemon spawns the floor (same-venv resolution, `python -m
voco.tts_floor` fallback), pipes its output into the daemon's own
stdout/stderr (one log stream, P4 formalizes it), restarts it on crash
with capped exponential backoff (reset after a healthy hour), and
tears it down on shutdown. Every exit and restart is emitted as a
daemon.error event — supervision is never silent.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 30.0
BACKOFF_RESET_S = 3600.0


def should_manage(tts_cfg: dict) -> int | None:
    """The floor's port when voco-d should supervise it, else None.
    Managed by default exactly when base_url is loopback on the floor's
    own port (8880); manage_floor=true/false overrides either way — but
    a non-loopback base_url is never managed (we won't supervise a
    process we can't own)."""
    from urllib.parse import urlsplit

    base = str(tts_cfg.get("base_url", "http://127.0.0.1:8880/v1"))
    try:
        u = urlsplit(base)
        host_ok = u.hostname in ("127.0.0.1", "localhost", "::1")
        port = u.port or 8880
    except ValueError:
        return None
    manage = tts_cfg.get("manage_floor")
    if manage is None:
        manage = host_ok and port == 8880
    if not manage or not host_ok:
        return None
    return port


def floor_argv(port: int) -> list[str]:
    exe = shutil.which("voco-tts-floor", path=str(Path(sys.executable).parent))
    argv = [exe] if exe else [sys.executable, "-m", "voco.tts_floor"]
    return [*argv, "--port", str(port)]


class FloorSupervisor:
    """Spawn/restart/stop one child process. Test seam: argv and the
    backoff constants are injectable."""

    def __init__(
        self,
        argv: list[str],
        # any emit works; the bus returns an Envelope we don't use
        emit: Callable[[str, dict], object],
        *,
        backoff_start: float = BACKOFF_START_S,
        backoff_cap: float = BACKOFF_CAP_S,
        backoff_reset: float = BACKOFF_RESET_S,
    ) -> None:
        self._argv = argv
        self._emit = emit
        self._backoff_start = backoff_start
        self._backoff_cap = backoff_cap
        self._backoff_reset = backoff_reset
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.restarts = 0  # observability (voco status / tests)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="tts-floor-supervisor")

    async def _spawn(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.DEVNULL,
            # the daemon's own stdout/stderr: one managed log stream
            stdout=None,
            stderr=None,
        )

    async def _run(self) -> None:
        backoff = self._backoff_start
        spawn_failures = 0
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                self._proc = await self._spawn()
                spawn_failures = 0
            except OSError as e:
                # spawn failures can be TRANSIENT (EMFILE, ENOMEM) —
                # retry through the same backoff, but a persistently
                # unspawnable argv gets a terminal give-up (xai P3 #8)
                spawn_failures += 1
                self._emit(
                    "daemon.error",
                    {
                        "error": (
                            f"tts floor failed to spawn ({e}) —"
                            f" attempt {spawn_failures}/5"
                        )
                    },
                )
                if spawn_failures >= 5:
                    self._emit(
                        "daemon.error",
                        {
                            "error": "tts floor: giving up after 5 spawn"
                            " failures — fix the install and restart voco-d"
                        },
                    )
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_cap)
                continue
            rc = await self._proc.wait()
            if self._stopping:
                return
            ran_s = asyncio.get_running_loop().time() - started
            if ran_s >= self._backoff_reset:
                backoff = self._backoff_start  # it was healthy for a while
            self.restarts += 1
            self._emit(
                "daemon.error",
                {
                    "error": (
                        f"tts floor exited rc={rc} after {ran_s:.0f}s —"
                        f" restarting in {backoff:.0f}s"
                        f" (restart #{self.restarts})"
                    )
                },
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_cap)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()  # loud last resort
            await proc.wait()
