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
from pathlib import Path

STATE_FILE = "registry.json"


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._path = state_dir / STATE_FILE

    @property
    def path(self) -> Path:
        return self._path

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
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(self._path)
