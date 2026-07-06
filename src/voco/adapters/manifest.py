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

import contextlib
import json
import os
import sys
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
    if sys.platform == "win32":
        # NEVER os.kill here: on Windows any non-CTRL signal — including
        # 0 — is TerminateProcess, i.e. the "liveness probe" would KILL
        # the lock holder. Query, don't touch.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
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
        naming the live holder. A dead/stale holder is taken over. The claim
        is atomic (O_CREAT|O_EXCL) so two daemons racing at startup cannot
        both win (review BLOCKER 4)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "start": _proc_start(os.getpid())})
        for _ in range(2):
            try:
                # open("x") is O_CREAT|O_EXCL portably — the raw os.open
                # flag/mode combination was observed failing on Windows
                # (live report: workbench persistence off on the primary
                # profile). The lock holds only pid + start nonce, so a
                # post-create chmod (best-effort, POSIX) suffices.
                with open(self._lock, "x", encoding="utf-8") as fh:
                    fh.write(payload)
            except FileExistsError:
                # A lock exists. If its holder is dead/stale, remove it and
                # retry the exclusive create; if it is live, refuse.
                if self._holder_is_live():
                    held = self._read_lock()
                    raise WorkspaceLockError(
                        f"another voco daemon (pid {held.get('pid')}) owns {self._dir}"
                    ) from None
                # Take over: unlink the stale lock and loop to re-create it.
                try:
                    self._lock.unlink()
                except FileNotFoundError:
                    pass  # someone else took it over first; retry sees theirs
                continue
            else:
                with contextlib.suppress(OSError):
                    os.chmod(self._lock, 0o600)
                return
        # Two rounds both lost the create race to a live holder.
        held = self._read_lock()
        raise WorkspaceLockError(
            f"another voco daemon (pid {held.get('pid')}) owns {self._dir}"
        )

    def _read_lock(self) -> dict:
        try:
            return json.loads(self._lock.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _holder_is_live(self) -> bool:
        held = self._read_lock()
        hpid = held.get("pid")
        return (
            isinstance(hpid, int)
            and hpid != os.getpid()
            and _pid_alive(hpid)
            and held.get("start") == _proc_start(hpid)
        )

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
        try:
            if os.name == "posix":
                # 0600 at create: no window with looser permissions
                # (proprietary review data, §8 sensitivity).
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, separators=(",", ":"))
            else:
                # Windows: mode bits don't map; plain create (ACLs apply).
                with open(tmp, "w", encoding="utf-8") as fh:
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
