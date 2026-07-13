"""voco.logsetup — one logging setup for the daemon (BUILD-PROD P4).

Structured and rotating: every daemon component logs through the
standard `voco.*` logger hierarchy, and setup() attaches exactly two
sinks — a RotatingFileHandler on daemon.log in the state dir
(~/.local/state/voco, $VOCO_STATE_DIR override: the same per-machine
service location the lifecycle commands use) and a stderr mirror for
foreground runs. Managed spawns (`voco up`, launchd) set
VOCO_LOG_CONSOLE=0 so the process-output capture file (daemon.out,
the crash net for pre-logging tracebacks) doesn't duplicate every
structured line.

INVARIANTS: setup() is idempotent (re-running replaces handlers, never
stacks them); an uncreatable state dir degrades to stderr-only with a
warning — logging trouble must never stop the daemon.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "daemon.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# C0 controls (minus \t) and DEL — covers ESC, so ANSI sequences lose
# their teeth and become visible residue instead of terminal commands.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class _SafeFormatter(logging.Formatter):
    """One record = one line, always (xai P4 round): floor output and
    daemon.error payloads carry untrusted text, so embedded newlines
    must not forge records and control characters must not drive the
    terminal `voco logs` prints to."""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        return _CONTROL.sub("", s.replace("\r", "\\r").replace("\n", "\\n"))


def state_dir() -> Path:
    """Mirrors voco_cli.lifecycle.state_dir: the CLI deliberately does
    not import the daemon package (it talks HTTP), so the default is
    duplicated; both honor $VOCO_STATE_DIR."""
    env = os.environ.get("VOCO_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "voco"


def setup(
    *,
    verbose: bool = False,
    log_dir: Path | None = None,
    console: bool | None = None,
    max_bytes: int = MAX_BYTES,
    backups: int = BACKUPS,
) -> Path | None:
    """Configure the `voco` logger tree; returns the log file path, or
    None when file logging is unavailable (the daemon still runs,
    logging to stderr, and the degradation is itself logged)."""
    root = logging.getLogger("voco")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False  # ours are the only sinks — no double lines
    for h in list(root.handlers):  # idempotent re-setup, never stacking
        root.removeHandler(h)
        h.close()
    fmt = _SafeFormatter(FORMAT, datefmt=DATEFMT)
    if console is None:
        console = os.environ.get("VOCO_LOG_CONSOLE", "1") != "0"
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    d = log_dir if log_dir is not None else state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            d / LOG_NAME, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
        return d / LOG_NAME
    except OSError as e:
        if not root.handlers:  # console was off AND the file failed
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        root.warning("file logging unavailable (%s) — stderr only", e)
        return None
