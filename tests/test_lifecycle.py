"""voco up/down/autostart plumbing (BUILD-PROD P1) — the pure helpers.

Process spawning is verified live (a daemon really started, health
really answered, SIGTERM really stopped it) per the campaign's rules;
these tests pin the logic that builds what the I/O layer runs."""

from __future__ import annotations

import os

from voco_cli import lifecycle


def test_daemon_argv_shapes():
    argv = lifecycle.daemon_argv(config=None, port=7777, no_audio=False)
    assert argv[-2:] == ["--port", "7777"]
    assert "--config" not in argv
    assert "--no-audio" not in argv
    argv = lifecycle.daemon_argv(config="/tmp/c.toml", port=7912, no_audio=True)
    assert argv[-1] == "--no-audio"
    i = argv.index("--config")
    assert argv[i + 1] == "/tmp/c.toml"
    assert argv[argv.index("--port") + 1] == "7912"


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    assert lifecycle.state_dir() == tmp_path / "sd"
    assert lifecycle.pidfile_path().parent == tmp_path / "sd"
    assert lifecycle.log_path().parent == tmp_path / "sd"


def test_read_pidfile_garbage_is_stale_not_fatal(tmp_path):
    p = tmp_path / "daemon.pid"
    assert lifecycle.read_pidfile(p) is None  # missing
    p.write_text("not-a-pid")
    assert lifecycle.read_pidfile(p) is None  # garbage
    p.write_text(" 4242 \n")
    assert lifecycle.read_pidfile(p) == 4242


def test_pid_alive_self_and_bogus():
    assert lifecycle.pid_alive(os.getpid()) is True
    # pid 2**22 + 1 is outside typical pid_max on macOS/Linux runners
    assert lifecycle.pid_alive(2**22 + 1) is False


def test_launchd_plist_contract(tmp_path):
    argv = ["/x/bin/voco-d", "--port", "7777"]
    log = tmp_path / "daemon.log"
    plist = lifecycle.build_launchd_plist(argv, log)
    # the four facts the agent depends on
    assert f"<string>{lifecycle.LAUNCHD_LABEL}</string>" in plist
    for a in argv:
        assert f"<string>{a}</string>" in plist
    assert "<key>RunAtLoad</key>" in plist and "<true/>" in plist
    # KeepAlive restarts crashes but NOT clean exits (`voco down` sticks)
    assert "<key>SuccessfulExit</key>" in plist
    assert str(log) in plist


def test_systemd_unit_mentions_argv_and_restart(tmp_path):
    unit = lifecycle.systemd_unit(["/x/voco-d", "--port", "7777"], tmp_path / "l.log")
    assert "ExecStart=/x/voco-d --port 7777" in unit
    assert "Restart=on-failure" in unit
