"""PTY terminal backend (SPEC-WORKBENCH §5, W4) — Unix.

ROLE: spawn a harness on a real pty owned by the daemon, fan its output
out to any number of workbench streams, keep a scrollback ring buffer
(the recovery source: reconnects replay it, clients that fall behind
drop frames and re-sync from it). asyncio-native: the master fd is
pumped with loop.add_reader — no reader threads.

INVARIANTS: the ring buffer bounds memory per terminal (default 256
KiB); subscriber queues bound per-client memory (drop-oldest — the ring
buffer, not the queue, is the source of truth); a dead process closes
every stream with an honest EOF; kill escalates SIGHUP → SIGKILL. v1 is
Unix-only — Windows ConPTY lands when it can be validated on Windows
(SPEC §11 W4 note; pywinpty floor check still pending).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass, field

RING_BYTES = 256 * 1024
SUBSCRIBER_FRAMES = 512  # per-client queue bound; beyond it, drop-oldest


class PtyError(Exception):
    """Soft, message-carrying failure — control surfaces it."""


@dataclass
class _Ring:
    """Bounded byte scrollback. Append-only; snapshot() is the replay."""

    limit: int = RING_BYTES
    _chunks: list[bytes] = field(default_factory=list)
    _size: int = 0

    def push(self, data: bytes) -> None:
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self.limit and self._chunks:
            drop = self._chunks.pop(0)
            self._size -= len(drop)

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)


class PtyProcess:
    """One spawned harness on a pty. Created via PtyBackend.spawn()."""

    def __init__(
        self,
        handle: str,
        proc: subprocess.Popen,
        master_fd: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.handle = handle
        self._proc = proc
        self._fd = master_fd
        self._loop = loop
        self._ring = _Ring()
        self._subs: set[asyncio.Queue[bytes | None]] = set()
        self._closed = False
        loop.add_reader(self._fd, self._on_readable)

    # ---- output fan-out -----------------------------------------------------

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._close()
            return
        self._ring.push(data)
        for q in list(self._subs):
            if q.qsize() >= SUBSCRIBER_FRAMES:
                # Drop-oldest: a stalled browser loses frames, never
                # stalls the daemon; its reconnect replays the ring.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(data)

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """A live output queue. First consume replay() yourself; a None
        sentinel means the terminal died."""
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        if self._closed:
            q.put_nowait(None)
        else:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes | None]) -> None:
        self._subs.discard(q)

    def replay(self) -> bytes:
        return self._ring.snapshot()

    def capture(self) -> str:
        """Point-in-time text view (watcher/peek parity with tmux)."""
        return self._ring.snapshot().decode("utf-8", errors="replace")

    # ---- input / control ------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self._closed:
            raise PtyError("terminal closed")
        os.write(self._fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        winsz = struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsz)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._proc.pid, signal.SIGWINCH)

    @property
    def alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    @property
    def pid(self) -> int:
        return self._proc.pid

    def kill(self) -> None:
        """SIGHUP the process group (a real hangup — shells exit
        cleanly), escalate to SIGKILL, release the pty. BLOCKS up to a
        few seconds — daemon shutdown and tests only; the live control
        path uses PtyBackend.akill (waits in the executor, never stalls
        the loop that pumps WS events and listens)."""
        self._terminate()
        self._close()

    def _terminate(self) -> None:
        """Signal + wait, no loop state touched — safe off-loop."""
        if self._proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGHUP)
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                self._proc.wait(timeout=5)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._fd)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        for q in list(self._subs):
            q.put_nowait(None)  # honest EOF to every stream
        self._subs.clear()


class PtyBackend:
    """Spawns and tracks PtyProcesses. The daemon owns one instance."""

    def __init__(self) -> None:
        self._procs: dict[str, PtyProcess] = {}
        self._counter = 0

    def spawn(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int = 120,
        rows: int = 32,
    ) -> PtyProcess:
        """Start `cmd` (shell line) on a fresh pty. The child gets the
        slave as its controlling terminal and its own process group so
        kill/hangup reach the whole tree."""
        loop = asyncio.get_running_loop()
        master, slave = os.openpty()
        winsz = struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)
        self._counter += 1
        handle = f"pty-{self._counter}"
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        full_env.setdefault("TERM", "xterm-256color")
        # The session-link: adapters derive identity.instance from this,
        # so the registered agent maps back to its daemon-owned terminal.
        full_env["VOCO_INSTANCE"] = handle
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=full_env,
                start_new_session=True,  # own pgid + lets us set ctty
                preexec_fn=_become_ctty_owner(slave),
            )
        except OSError as e:
            os.close(master)
            raise PtyError(f"pty spawn failed: {e}") from e
        finally:
            with contextlib.suppress(OSError):
                os.close(slave)
        os.set_blocking(master, False)
        pp = PtyProcess(handle, proc, master, loop)
        self._procs[handle] = pp
        return pp

    def get(self, handle: str) -> PtyProcess | None:
        return self._procs.get(handle)

    def kill(self, handle: str) -> None:
        pp = self._procs.pop(handle, None)
        if pp is None:
            raise PtyError(f"no such terminal: {handle}")
        pp.kill()

    async def akill(self, handle: str) -> None:
        """kill() for the live control path: the terminate wait runs in
        the executor; only the (cheap) fd/subscriber cleanup touches the
        loop."""
        pp = self._procs.pop(handle, None)
        if pp is None:
            raise PtyError(f"no such terminal: {handle}")
        await asyncio.get_running_loop().run_in_executor(None, pp._terminate)
        pp._close()

    def shutdown(self) -> None:
        for handle in list(self._procs):
            with contextlib.suppress(Exception):
                self.kill(handle)


def _become_ctty_owner(slave_fd: int):
    """preexec_fn: make the pty slave the child's controlling terminal
    (job control + SIGWINCH work like a real terminal)."""

    def _inner() -> None:
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    return _inner
