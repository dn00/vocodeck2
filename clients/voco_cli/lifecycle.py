"""voco lifecycle — `voco up|down|logs|autostart` (BUILD-PROD P1).

ROLE: own the daemon PROCESS so nobody hand-runs `uv run voco-d` in a
forgotten terminal again. Design:

- `voco up`: idempotent start. Health-probe first; spawn `voco-d`
  detached (own session, output to the managed log), write a pidfile,
  wait for health, report honestly (on failure: the log tail).
- `voco down`: pidfile → SIGTERM → bounded wait → SIGKILL only as a
  loudly-reported last resort. Refuses to guess when it finds a daemon
  it didn't start.
- `voco logs [-f]`: the managed log, tail or follow.
- `voco autostart install|uninstall|status`: launchd agent on macOS
  (KeepAlive on crash, RunAtLoad); prints a systemd --user unit as
  guidance elsewhere.

Lifecycle files live in the DEFAULT state dir (~/.local/state/voco or
$VOCO_STATE_DIR): the pidfile/log are per-machine service facts, not
per-config state, so a custom [state].dir never orphans them.

Pure helpers (argv/plist/pid parsing) are separated from I/O so tests
cover the logic without spawning processes.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LAUNCHD_LABEL = "io.voco.daemon"


def state_dir() -> Path:
    env = os.environ.get("VOCO_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "voco"


def pidfile_path() -> Path:
    return state_dir() / "daemon.pid"


def log_path() -> Path:
    return state_dir() / "daemon.log"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


# ---- pure helpers (unit-tested) ------------------------------------------------


def daemon_argv(*, config: str | None, port: int, no_audio: bool) -> list[str]:
    """The exact argv `voco up` / launchd runs. Prefers the installed
    voco-d next to this interpreter (same venv), falls back to
    `python -m voco.daemon` so a source checkout works too."""
    exe = shutil.which("voco-d", path=str(Path(sys.executable).parent))
    argv = [exe] if exe else [sys.executable, "-m", "voco.daemon"]
    if config:
        argv += ["--config", config]
    argv += ["--port", str(port)]
    if no_audio:
        argv += ["--no-audio"]
    return argv


def read_pidfile(path: Path) -> int | None:
    """The pid, or None for missing/garbage (garbage is treated as
    stale, never an exception — a corrupt pidfile must not wedge
    `voco down`)."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, someone else's — still alive
    return True


def build_launchd_plist(argv: list[str], log: Path) -> str:
    """KeepAlive on crash (not on clean exit — `voco down` must stick),
    RunAtLoad, both output streams to the managed log."""
    args_xml = "\n".join(f"      <string>{a}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
  </dict>
</plist>
"""


def systemd_unit(argv: list[str], log: Path) -> str:
    """Printed as guidance on non-macOS (P1 documents; a managed
    install can follow when a Linux daily-driver exists)."""
    return f"""[Unit]
Description=voco daemon

[Service]
ExecStart={" ".join(argv)}
Restart=on-failure
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


# ---- health ---------------------------------------------------------------------


def healthy(base_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _tail(path: Path, n: int = 15) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(no log yet)"


# ---- commands -------------------------------------------------------------------


def cmd_up(args, base_url: str) -> int:
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    if healthy(base_url):
        print(f"voco: daemon already running at {base_url}")
        return 0
    pid = read_pidfile(pidfile_path())
    if pid is not None and pid_alive(pid):
        print(
            f"voco: pid {pid} is alive but {base_url} is not answering —"
            " it may still be starting; `voco logs` to look"
        )
        return 1
    argv = daemon_argv(config=args.config, port=args.port, no_audio=args.no_audio)
    log = log_path()
    with open(log, "a", encoding="utf-8") as lf:
        lf.write(f"\n--- voco up · {time.strftime('%F %T')} ---\n")
        lf.flush()
        proc = subprocess.Popen(
            argv,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives this CLI and its terminal
        )
    pidfile_path().write_text(str(proc.pid))
    deadline = time.time() + float(args.wait)
    while time.time() < deadline:
        if healthy(base_url):
            print(f"voco: daemon up at {base_url} (pid {proc.pid}, log: {log})")
            return 0
        if proc.poll() is not None:
            break  # died during boot — report the log, don't spin
        time.sleep(0.3)
    print(f"voco: daemon did not become healthy — last log lines:\n{_tail(log)}")
    return 1


def cmd_down(base_url: str) -> int:
    pid = read_pidfile(pidfile_path())
    if pid is None or not pid_alive(pid):
        if healthy(base_url):
            print(
                "voco: a daemon answers but was not started by `voco up`"
                " (no live pidfile) — stop it where it was started,"
                " or use `voco autostart uninstall` if launchd owns it"
            )
            return 1
        print("voco: no daemon running")
        pidfile_path().unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 15
    while time.time() < deadline:
        if not pid_alive(pid):
            pidfile_path().unlink(missing_ok=True)
            print(f"voco: daemon stopped (pid {pid})")
            return 0
        time.sleep(0.3)
    os.kill(pid, signal.SIGKILL)  # loud last resort — never silent
    pidfile_path().unlink(missing_ok=True)
    print(
        f"voco: daemon (pid {pid}) ignored SIGTERM for 15s — killed."
        " If this repeats, `voco logs` and report it."
    )
    return 1


def cmd_logs(args) -> int:
    log = log_path()
    if not log.exists():
        print(f"voco: no log yet at {log}")
        return 1
    print(_tail(log, args.lines))
    if not args.follow:
        return 0
    with open(log, encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def cmd_autostart(args, base_url: str) -> int:
    argv = daemon_argv(config=args.config, port=args.port, no_audio=False)
    if platform.system() != "Darwin":
        print("voco: managed autostart is macOS/launchd for now;")
        print("systemd --user unit to adapt:\n")
        print(systemd_unit(argv, log_path()))
        return 1
    plist = launchd_plist_path()
    if args.action == "status":
        loaded = (
            subprocess.run(
                ["launchctl", "list", LAUNCHD_LABEL],
                capture_output=True,
            ).returncode
            == 0
        )
        print(
            f"voco: autostart {'installed' if plist.exists() else 'not installed'}"
            f" · {'loaded' if loaded else 'not loaded'} · {plist}"
        )
        return 0
    if args.action == "install":
        state_dir().mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(build_launchd_plist(argv, log_path()))
        # bootout first = idempotent reinstall picks up argv changes
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
            capture_output=True,
        )
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(f"voco: launchctl bootstrap failed: {r.stderr.strip()}")
            return 1
        print(
            f"voco: autostart installed ({plist}); daemon starts at login"
            " and restarts on crash"
        )
        return 0
    if args.action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
            capture_output=True,
        )
        plist.unlink(missing_ok=True)
        print(
            "voco: autostart uninstalled (a running daemon keeps running;"
            " `voco down` stops it)"
        )
        return 0
    print(f"voco: unknown autostart action {args.action!r}")
    return 2
