"""voco up/down/autostart plumbing (BUILD-PROD P1, hardened per the
adversarial review) — the pure helpers.

Process spawning is verified live (a daemon really started, health
really answered, SIGTERM really stopped it); these tests pin the logic
the I/O layer runs — especially the review's blockers: plist injection,
PID-reuse identity, stale-pidfile hygiene."""

from __future__ import annotations

import os
import plistlib

from voco_cli import lifecycle


def test_daemon_argv_shapes():
    argv = lifecycle.daemon_argv(config=None, port=7777, no_audio=False)
    assert argv[-2:] == ["--port", "7777"]
    assert "--config" not in argv
    assert "--no-audio" not in argv
    argv = lifecycle.daemon_argv(config="/tmp/c.toml", port=7912, no_audio=True)
    assert argv[-1] == "--no-audio"
    assert argv[argv.index("--config") + 1] == "/tmp/c.toml"
    assert argv[argv.index("--port") + 1] == "7912"


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    assert lifecycle.state_dir() == tmp_path / "sd"
    assert lifecycle.pidfile_path().parent == tmp_path / "sd"
    assert lifecycle.log_path().parent == tmp_path / "sd"


def test_local_url_ignores_voco_url(monkeypatch):
    # VOCO_URL aims clients (possibly remote); signals must stay local.
    monkeypatch.setenv("VOCO_URL", "http://evil.example:7777")
    assert lifecycle.local_url(7913) == "http://127.0.0.1:7913"


def test_read_pidfile_garbage_is_stale_not_fatal(tmp_path):
    p = tmp_path / "daemon.pid"
    assert lifecycle.read_pidfile(p) is None  # missing
    p.write_text("not-a-pid")
    assert lifecycle.read_pidfile(p) is None  # garbage
    p.write_text(" 4242 \n")
    assert lifecycle.read_pidfile(p) == 4242


def test_looks_like_voco_identity():
    # PID reuse: only provably-voco commands may be signaled
    assert lifecycle.looks_like_voco("/venv/bin/voco-d --port 7777")
    # console-script shim: ps shows python as argv0 (live-drill bug)
    assert lifecycle.looks_like_voco("/venv/bin/python /venv/bin/voco-d --port 7777")
    assert lifecycle.looks_like_voco("python -m voco.daemon --port 7777")
    assert not lifecycle.looks_like_voco("/usr/bin/postgres -D /data")
    assert not lifecycle.looks_like_voco("vim voco-design.md")
    assert not lifecycle.looks_like_voco("python train.py voco-d")  # tail junk


def test_managed_pid_cleans_stale_pidfile(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path))
    pf = lifecycle.pidfile_path()
    pf.parent.mkdir(parents=True, exist_ok=True)
    # our own pid is alive but is NOT voco-d → stale, cleaned, None
    pf.write_text(str(os.getpid()))
    assert lifecycle.managed_pid() is None
    assert not pf.exists()


def test_launchd_plist_is_valid_and_injection_proof(tmp_path):
    # the review's #1 blocker: XML metacharacters in argv/paths
    nasty = '/tmp/<evil>&"quo".toml'
    argv = ["/x/bin/voco-d", "--config", nasty, "--port", "7777"]
    log = tmp_path / "dae&mon.log"
    data = lifecycle.build_launchd_plist(argv, log)
    parsed = plistlib.loads(data)  # must round-trip as REAL plist data
    assert parsed["Label"] == lifecycle.LAUNCHD_LABEL
    assert parsed["ProgramArguments"] == argv  # nasty string intact
    assert parsed["RunAtLoad"] is True
    # KeepAlive restarts crashes but NOT clean exits (`voco down` sticks)
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["StandardOutPath"] == str(log)


def test_systemd_unit_quotes_spaces(tmp_path):
    unit = lifecycle.systemd_unit(
        ["/Applications/My Tools/voco-d", "--port", "7777"], tmp_path / "l.log"
    )
    assert "'/Applications/My Tools/voco-d' --port 7777" in unit
    assert "Restart=on-failure" in unit


def test_tail_text_last_lines_without_full_read(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("\n".join(f"line{i}" for i in range(1000)) + "\n")
    out = lifecycle.tail_text(p, 3)
    assert out.splitlines() == ["line997", "line998", "line999"]
    assert lifecycle.tail_text(tmp_path / "missing.log", 3) == "(no log yet)"
