"""Registry state on disk (durable sessions across daemon restarts).

ROLE: the fs edge for core.registry dump/restore — one JSON snapshot,
written atomically (tmp + replace) at mode 0600 because session_ids are
capability tokens. The daemon decides WHEN to save (debounced on bus
events + on shutdown); this module only knows HOW.

INVARIANTS: load never raises — a corrupt file is renamed to *.corrupt
and reported as an error string so the daemon can route it to
daemon.error and boot fresh; save failures raise to the caller (the
daemon routes them — errors are never swallowed here).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from voco.adapters.manifest import _pid_alive, _proc_start, _sync_dir

STATE_FILE = "registry.json"


class StateLockError(Exception):
    """Another live daemon already owns this registry state directory."""


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._path = state_dir / STATE_FILE
        self._lock = state_dir / "registry.lock"

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, wait_s: float = 0.0) -> None:
        """Exclusively claim registry persistence for this daemon run."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                self._acquire_once()
                return
            except StateLockError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)

    def _read_lock(self) -> dict:
        try:
            return json.loads(self._lock.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _holder_is_live(self) -> bool:
        held = self._read_lock()
        pid = held.get("pid")
        return (
            isinstance(pid, int)
            and pid != os.getpid()
            and _pid_alive(pid)
            and held.get("start") == _proc_start(pid)
        )

    def _acquire_once(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "start": _proc_start(os.getpid())})
        for _ in range(2):
            try:
                with open(self._lock, "x", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            except FileExistsError:
                if self._holder_is_live():
                    held = self._read_lock()
                    raise StateLockError(
                        f"another voco daemon (pid {held.get('pid')}) owns {self._dir}"
                    ) from None
                self._lock.unlink(missing_ok=True)
                continue
            else:
                if os.name == "posix":
                    os.chmod(self._lock, 0o600)
                _sync_dir(self._dir)
                return
        held = self._read_lock()
        raise StateLockError(
            f"another voco daemon (pid {held.get('pid')}) owns {self._dir}"
        )

    def release(self) -> None:
        try:
            held = self._read_lock()
            if held.get("pid") == os.getpid():
                self._lock.unlink(missing_ok=True)
                _sync_dir(self._dir)
        except OSError:
            pass

    def load(self) -> tuple[dict | None, str | None]:
        """Returns (data, error). Missing file is (None, None) — fresh boot."""
        if not self._path.exists():
            return None, None
        try:
            return json.loads(self._path.read_text(encoding="utf-8")), None
        except (json.JSONDecodeError, OSError) as e:
            corrupt = self._path.with_suffix(".corrupt")
            try:
                self._path.replace(corrupt)
            except OSError:
                pass  # named fail-silent: the sidecar rename is best-effort;
                # the load error below is what the operator acts on
            return None, f"state file unreadable ({e}); moved to {corrupt.name}"

    def save(self, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(self._path)
        _sync_dir(self._dir)
