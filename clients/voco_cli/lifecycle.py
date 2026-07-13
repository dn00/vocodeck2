"""voco lifecycle — `voco up|down|logs|autostart` (BUILD-PROD P1).

ROLE: own the daemon PROCESS so nobody hand-runs `uv run voco-d` in a
forgotten terminal again. Hardened per the P1 adversarial review:

- pid identity: a pidfile number is only trusted after `ps` confirms
  the process actually looks like voco-d (PID reuse after crash/reboot
  must never get an unrelated process SIGKILLed).
- `voco up` takes an exclusive spawn lock (O_EXCL, stale after 60s) so
  two concurrent ups can't double-spawn; failure paths clean the
  pidfile; success requires OUR child alive AND a voco-signed health
  response (a random 200 on the port must not read as success).
- plists are built with plistlib (never f-string XML — config paths
  with XML metacharacters were an injection) and written atomically;
  the systemd guidance uses shlex quoting.
- POSIX-only, honestly: on Windows these commands say so and exit.
- Lifecycle URLs are always local (127.0.0.1:port): VOCO_URL points
  clients at daemons, possibly remote — it must never aim `down`.

Lifecycle files live in the DEFAULT state dir (~/.local/state/voco or
$VOCO_STATE_DIR): pidfile/log are per-machine service facts, not
per-config state. Two log files by design (P4): daemon.log is the
daemon's OWN structured rotating log (voco.logsetup writes it; `voco
logs` reads it, rotation-aware), while daemon.out is the spawn capture
— the crash net for pre-logging tracebacks and anything that hits raw
stdout/stderr. Managed spawns set VOCO_LOG_CONSOLE=0 so daemon.out
doesn't duplicate every structured line.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

LAUNCHD_LABEL = "io.voco.daemon"
UP_LOCK_STALE_S = 60


def state_dir() -> Path:
    env = os.environ.get("VOCO_STATE_DIR")
    return Path(env) if env else Path.home() / ".local" / "state" / "voco"


def pidfile_path() -> Path:
    return state_dir() / "daemon.pid"


def log_path() -> Path:
    """The daemon's structured rotating log (voco.logsetup owns it)."""
    return state_dir() / "daemon.log"


def out_path() -> Path:
    """The spawn's raw stdout/stderr capture — pre-logging crash net."""
    return state_dir() / "daemon.out"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def local_url(port: int) -> str:
    """Lifecycle is local by nature — deliberately ignores VOCO_URL."""
    return f"http://127.0.0.1:{port}"


def _posix_or_explain() -> bool:
    if os.name == "posix":
        return True
    print(
        "voco: managed lifecycle (up/down/logs) is POSIX-only for now —"
        " on Windows run voco-d directly; a service wrapper is on the"
        " roadmap (BUILD-PROD)."
    )
    return False


# ---- pure helpers (unit-tested) ------------------------------------------------


def daemon_argv(
    *, config: str | None, port: int, no_audio: bool, verbose: bool = False
) -> list[str]:
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
    if verbose:
        argv += ["--verbose"]
    return argv


def read_pidfile(path: Path) -> int | None:
    """The pid, or None for missing/garbage (garbage is treated as
    stale, never an exception — a corrupt pidfile must not wedge
    `voco down`)."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def looks_like_voco(cmdline: str) -> bool:
    """Identity test for a pidfile pid: the command must actually be
    voco-d (binary or `python -m voco.daemon`). Token-exact on argv0's
    basename — a substring test let `vim voco-design.md` read as the
    daemon (caught by our own test). PID reuse must never aim a signal
    at an innocent process."""
    parts = cmdline.split()
    # console-script shims run as `python /venv/bin/voco-d …`, so the
    # voco-d token can be argv0 OR argv1 (live drill caught argv0-only)
    if any(Path(tok).name == "voco-d" for tok in parts[:2]):
        return True
    return "voco.daemon" in parts  # the `python -m voco.daemon` form


def pid_cmdline(pid: int) -> str | None:
    """`ps` read of the process command. None = the process does not
    exist; "" = the PROBE failed (ps unavailable/timed out) — unknown
    is not the same as gone, and callers must not destroy state on it."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def managed_pid() -> int | None:
    """The pidfile pid IF it is alive and provably voco. A stale or
    hijacked-by-reuse pidfile is cleaned; an UNKNOWN read (probe
    failure) keeps the pidfile — a transient ps hiccup must never
    orphan a healthy daemon (live drill lesson)."""
    pid = read_pidfile(pidfile_path())
    if pid is None:
        return None
    cmd = pid_cmdline(pid)
    if cmd == "":
        return None  # unknown — act unmanaged, but keep the record
    if cmd is None or not looks_like_voco(cmd):
        pidfile_path().unlink(missing_ok=True)  # stale — never trust again
        return None
    return pid


def _service_env() -> dict[str, str]:
    """Env a managed daemon runs with: console mirror off (daemon.out
    is the raw-output crash net, not a duplicate of daemon.log), and a
    custom $VOCO_STATE_DIR carried through — the plist's paths were
    computed from it, so the daemon must resolve the same state dir."""
    env = {"VOCO_LOG_CONSOLE": "0"}
    custom = os.environ.get("VOCO_STATE_DIR")
    if custom:
        env["VOCO_STATE_DIR"] = custom
    return env


def build_launchd_plist(argv: list[str], out: Path) -> bytes:
    """plistlib, not string XML: argv/paths are user-controlled and XML
    metacharacters in a config path were an injection. KeepAlive on
    crash only (clean exits stick, so `voco down` sticks). launchd
    captures raw stdout/stderr into daemon.out; the daemon writes its
    own rotating daemon.log, so the console mirror is off (P4)."""
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": list(argv),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(out),
            "StandardErrorPath": str(out),
            "EnvironmentVariables": _service_env(),
        }
    )


def systemd_unit(argv: list[str], out: Path) -> str:
    """Printed as guidance on non-macOS; shlex-quoted so paths with
    spaces produce a valid ExecStart."""
    env_lines = "\n".join(
        f"Environment={shlex.quote(f'{k}={v}')}" for k, v in _service_env().items()
    )
    return f"""[Unit]
Description=voco daemon

[Service]
ExecStart={shlex.join(argv)}
Restart=on-failure
{env_lines}
StandardOutput=append:{out}
StandardError=append:{out}

[Install]
WantedBy=default.target
"""


def tail_text(path: Path, n: int) -> str:
    """Last n lines without reading the whole file (rotation caps
    daemon.log, but daemon.out and foreign logs can still be large)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 131072))
            data = f.read().decode(errors="replace")
    except OSError:
        return "(no log yet)"
    lines = data.splitlines()
    return "\n".join(lines[-n:])


# ---- health ---------------------------------------------------------------------


def healthy(base_url: str, timeout: float = 2.0) -> bool:
    """GET /v1/health: 200 + service=="voco-d" (P4). The endpoint
    ANSWERING is authoritative: a different service name, garbage JSON,
    or an error status is a hard no (a real voco-d never sends those).
    Only endpoint-absent (404/405 from a pre-P4 daemon mid-upgrade) or
    a connection-level failure falls through to the P1 body-signature
    heuristic — and with nothing listening, that fails too."""
    try:
        with urllib.request.urlopen(base_url + "/v1/health", timeout=timeout) as r:
            if r.status != 200:
                return False
            try:
                data = json.loads(r.read(4096).decode(errors="replace"))
            except ValueError:
                return False  # a squatter's 200; voco-d sends real JSON
            return data.get("service") == "voco-d"
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            return False  # the endpoint exists and is not healthy voco
    except Exception:
        pass  # connection-level: let the legacy probe decide
    try:
        with urllib.request.urlopen(base_url + "/", timeout=timeout) as r:
            if r.status != 200:
                return False
            body = r.read(4096).decode(errors="replace")
            return "voco" in body
    except Exception:
        return False


# ---- commands -------------------------------------------------------------------


def cmd_up(args) -> int:
    if not _posix_or_explain():
        return 1
    base_url = local_url(args.port)
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    if healthy(base_url):
        print(f"voco: daemon already running at {base_url}")
        return 0
    pid = managed_pid()
    if pid is not None:
        print(
            f"voco: managed daemon (pid {pid}) is alive but {base_url} is"
            " not answering — it may still be starting, or it runs on a"
            " different --port; `voco logs` to look"
        )
        return 1
    # Exclusive spawn lock: two concurrent `voco up` must not double-
    # spawn. Stale locks (crashed CLI) expire after UP_LOCK_STALE_S.
    lock = sd / "up.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > UP_LOCK_STALE_S:
            lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        print("voco: another `voco up` is already running — waiting is safer")
        return 1
    except OSError as e:
        print(f"voco: cannot take the spawn lock: {e}")
        return 1
    try:
        argv = daemon_argv(
            config=args.config,
            port=args.port,
            no_audio=args.no_audio,
            verbose=getattr(args, "verbose", False),
        )
        log, out = log_path(), out_path()
        with open(out, "a", encoding="utf-8") as of:
            of.write(f"\n--- voco up · {time.strftime('%F %T')} ---\n")
            of.flush()
            proc = subprocess.Popen(
                argv,
                stdout=of,
                stderr=of,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # survives this CLI and its terminal
                # the daemon writes its own rotating daemon.log; daemon.out
                # captures only raw output (crashes before logging is up)
                env={**os.environ, "VOCO_LOG_CONSOLE": "0"},
            )
        pidfile_path().write_text(str(proc.pid))
        deadline = time.time() + max(1.0, float(args.wait))
        while time.time() < deadline:
            # success = OUR child alive AND a voco-signed answer — a 200
            # from something else must not claim this pid succeeded
            if proc.poll() is not None:
                break
            if healthy(base_url):
                print(f"voco: daemon up at {base_url} (pid {proc.pid}, log: {log})")
                return 0
            time.sleep(0.3)
        pidfile_path().unlink(missing_ok=True)  # failed boot: never leave bait
        print("voco: daemon did not become healthy —")
        print(f"--- process output ({out}) ---\n{tail_text(out, 10)}")
        print(f"--- daemon log ({log}) ---\n{tail_text(log, 15)}")
        return 1
    finally:
        lock.unlink(missing_ok=True)


def _launchd_loaded() -> bool:
    if platform.system() != "Darwin":
        return False
    return (
        subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL], capture_output=True
        ).returncode
        == 0
    )


def cmd_down(args) -> int:
    if not _posix_or_explain():
        return 1
    base_url = local_url(args.port)
    pid = managed_pid()
    if pid is None:
        if _launchd_loaded():
            print(
                "voco: launchd owns the daemon — `voco autostart uninstall`"
                " stops and removes it (or `launchctl bootout"
                f" gui/{os.getuid()}/{LAUNCHD_LABEL}` to stop once)"
            )
            return 1
        if healthy(base_url):
            print(
                "voco: a daemon answers but was not started by `voco up`"
                " — stop it where it was started"
            )
            return 1
        print("voco: no daemon running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        print(
            f"voco: pid {pid} exists but belongs to another user — not"
            " touching it; remove the pidfile yourself if it is stale:"
            f" {pidfile_path()}"
        )
        return 1
    except ProcessLookupError:
        pidfile_path().unlink(missing_ok=True)
        print("voco: daemon already gone")
        return 0
    deadline = time.time() + 15
    while time.time() < deadline:
        if pid_cmdline(pid) is None:
            pidfile_path().unlink(missing_ok=True)
            print(f"voco: daemon stopped (pid {pid})")
            return 0
        time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)  # loud last resort — never silent
    except (ProcessLookupError, PermissionError):
        pass
    pidfile_path().unlink(missing_ok=True)
    print(
        f"voco: daemon (pid {pid}) ignored SIGTERM for 15s — killed."
        " If this repeats, `voco logs` and report it."
    )
    return 1


def follow_lines(
    path: Path, *, poll_s: float = 0.5, sleep: Callable[[float], None] = time.sleep
) -> Iterator[str]:
    """Yield appended lines forever, surviving rotation (P4): when the
    file at `path` is replaced (inode change) or truncated (size below
    our position), reopen and read the NEW file from its start. A
    missing file — before first boot or mid-rotation — is a normal
    gap to wait through, not an error."""
    waited = False
    while True:
        try:
            # not a with-block: replaced on rotation (closed in finally)
            f = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
            break
        except FileNotFoundError:
            waited = True
            sleep(poll_s)  # no log yet — wait for its birth
    try:
        if not waited:  # tail semantics for an existing file; a file we
            f.seek(0, os.SEEK_END)  # waited for is read from its first line
        ino = os.fstat(f.fileno()).st_ino
        while True:
            line = f.readline()
            if line:
                yield line
                continue
            try:
                st = os.stat(path)
            except OSError:
                sleep(poll_s)  # rotation gap: old renamed, new not yet there
                continue
            if st.st_ino != ino or st.st_size < f.tell():
                f.close()
                f = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
                ino = os.fstat(f.fileno()).st_ino
                continue
            sleep(poll_s)
    finally:
        f.close()


def cmd_logs(args) -> int:
    if not _posix_or_explain():
        return 1
    log = log_path()
    if not log.exists():
        print(f"voco: no log yet at {log}")
        return 1
    print(tail_text(log, args.lines))
    if not args.follow:
        return 0
    try:
        for line in follow_lines(log):
            print(line, end="")
    except KeyboardInterrupt:
        pass
    return 0


def cmd_autostart(args) -> int:
    argv = daemon_argv(config=args.config, port=args.port, no_audio=False)
    if platform.system() != "Darwin":
        print("voco: managed autostart is macOS/launchd for now;")
        print("systemd --user unit to adapt:\n")
        print(systemd_unit(argv, out_path()))
        return 1
    plist = launchd_plist_path()
    if args.action == "status":
        print(
            f"voco: autostart {'installed' if plist.exists() else 'not installed'}"
            f" · {'loaded' if _launchd_loaded() else 'not loaded'} · {plist}"
        )
        return 0
    if args.action == "install":
        state_dir().mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        tmp = plist.with_suffix(".plist.tmp")
        tmp.write_bytes(build_launchd_plist(argv, out_path()))
        tmp.replace(plist)  # atomic — a half-written plist must not load
        # bootout first = idempotent reinstall picks up argv changes;
        # its failure is EXPECTED when nothing was loaded, so quiet.
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
        was_loaded = _launchd_loaded()
        r = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
            capture_output=True,
            text=True,
        )
        if was_loaded and r.returncode != 0:
            # a loaded job that refuses to unload is a real failure
            print(f"voco: launchctl bootout failed: {r.stderr.strip()}")
            return 1
        plist.unlink(missing_ok=True)
        print("voco: autostart uninstalled")
        return 0
    print(f"voco: unknown autostart action {args.action!r}")
    return 2
