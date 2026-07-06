"""Workspace manifests on disk (SPEC-WORKBENCH §8) — durable pages +
findings across daemon restarts.

ROLE: the fs edge for WorkspaceStore. One manifest.json per workspace under
`<data_dir>/workspaces/<safe-key>/` (the same dir exports land in), written
atomically at 0600 (proprietary review data). A daemon-level single-writer
lock guards the data dir: voco is ONE daemon hosting ALL workspaces (unlike
diff-annotate's per-workspace servers), so one lock is the correct shape —
it stops a second daemon from clobbering saves. The lock carries pid + a
process start-time nonce so a reused pid cannot masquerade as the holder.

INVARIANTS: load never raises (a corrupt manifest is skipped + reported);
save failures raise to the caller (the daemon routes them). The daemon
decides WHEN to save (debounced on bus events + on shutdown).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def safe_key(key: str) -> str:
    return key.replace("/", "%2F").replace(":", "%3A")


def _proc_start(pid: int) -> str | None:
    """A start-time nonce so a reused pid is not mistaken for the holder.
    Linux /proc; None elsewhere (degrades to pid-only, like the registry)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WorkspaceLockError(Exception):
    """Another live daemon already owns this data dir."""


class WorkspaceManifest:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._ws_dir = data_dir / "workspaces"
        self._lock = data_dir / "daemon.lock"

    # ---- single-writer lock -------------------------------------------------

    def acquire(self) -> None:
        """Claim the data dir for this process, or raise WorkspaceLockError
        naming the live holder. A dead/stale holder is taken over."""
        self._dir.mkdir(parents=True, exist_ok=True)
        me = {"pid": os.getpid(), "start": _proc_start(os.getpid())}
        if self._lock.exists():
            try:
                held = json.loads(self._lock.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                held = {}
            hpid = held.get("pid")
            live = (
                isinstance(hpid, int)
                and hpid != os.getpid()
                and _pid_alive(hpid)
                and held.get("start") == _proc_start(hpid)
            )
            if live:
                raise WorkspaceLockError(
                    f"another voco daemon (pid {hpid}) owns {self._dir}"
                )
        self._lock.write_text(json.dumps(me), encoding="utf-8")

    def release(self) -> None:
        try:
            held = json.loads(self._lock.read_text(encoding="utf-8"))
            if held.get("pid") == os.getpid():
                self._lock.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            pass

    # ---- per-workspace manifests --------------------------------------------

    def _path(self, key: str) -> Path:
        return self._ws_dir / safe_key(key) / "manifest.json"

    def save(self, key: str, data: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(path)

    def load_all(self) -> tuple[list[dict], list[str]]:
        """Return (manifests, errors). Missing dir → empty, fresh boot."""
        out: list[dict] = []
        errors: list[str] = []
        if not self._ws_dir.is_dir():
            return out, errors
        for sub in sorted(self._ws_dir.iterdir()):
            mpath = sub / "manifest.json"
            if not mpath.exists():
                continue
            try:
                out.append(json.loads(mpath.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                errors.append(f"{mpath.name} unreadable ({e})")
        return out, errors
