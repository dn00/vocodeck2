"""voco up/down/autostart plumbing (BUILD-PROD P1, hardened per the
adversarial review) — the pure helpers.

Process spawning is verified live (a daemon really started, health
really answered, SIGTERM really stopped it); these tests pin the logic
the I/O layer runs — especially the review's blockers: plist injection,
PID-reuse identity, stale-pidfile hygiene."""

from __future__ import annotations

import http.server
import os
import plistlib
import threading
from typing import ClassVar

import pytest

from voco_cli import lifecycle


def test_daemon_argv_shapes():
    argv = lifecycle.daemon_argv(config=None, port=7777, no_audio=False)
    assert argv[-2:] == ["--port", "7777"]
    assert "--config" not in argv
    assert "--no-audio" not in argv
    assert "--verbose" not in argv
    argv = lifecycle.daemon_argv(config="/tmp/c.toml", port=7912, no_audio=True)
    assert argv[-1] == "--no-audio"
    assert argv[argv.index("--config") + 1] == "/tmp/c.toml"
    assert argv[argv.index("--port") + 1] == "7912"
    argv = lifecycle.daemon_argv(config=None, port=7777, no_audio=False, verbose=True)
    assert "--verbose" in argv


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    assert lifecycle.state_dir() == tmp_path / "sd"
    assert lifecycle.pidfile_path().parent == tmp_path / "sd"
    assert lifecycle.log_path().parent == tmp_path / "sd"
    assert lifecycle.out_path().parent == tmp_path / "sd"
    # two files by design: the daemon's rotating log vs the spawn capture
    assert lifecycle.log_path() != lifecycle.out_path()


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


def test_launchd_plist_is_valid_and_injection_proof(tmp_path, monkeypatch):
    # the review's #1 blocker: XML metacharacters in argv/paths
    monkeypatch.delenv("VOCO_STATE_DIR", raising=False)  # default env shape
    nasty = '/tmp/<evil>&"quo".toml'
    argv = ["/x/bin/voco-d", "--config", nasty, "--port", "7777"]
    out = tmp_path / "dae&mon.out"
    data = lifecycle.build_launchd_plist(argv, out)
    parsed = plistlib.loads(data)  # must round-trip as REAL plist data
    assert parsed["Label"] == lifecycle.LAUNCHD_LABEL
    assert parsed["ProgramArguments"] == argv  # nasty string intact
    assert parsed["RunAtLoad"] is True
    # KeepAlive restarts crashes but NOT clean exits (`voco down` sticks)
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    # launchd captures raw output into daemon.out; the daemon writes its
    # own rotating daemon.log with the console mirror off (P4)
    assert parsed["StandardOutPath"] == str(out)
    assert parsed["StandardErrorPath"] == str(out)
    assert parsed["EnvironmentVariables"] == {"VOCO_LOG_CONSOLE": "0"}


def test_systemd_unit_quotes_spaces(tmp_path):
    unit = lifecycle.systemd_unit(
        ["/Applications/My Tools/voco-d", "--port", "7777"], tmp_path / "l.out"
    )
    assert "'/Applications/My Tools/voco-d' --port 7777" in unit
    assert "Restart=on-failure" in unit
    assert "Environment=VOCO_LOG_CONSOLE=0" in unit


def test_tail_text_last_lines_without_full_read(tmp_path):
    p = tmp_path / "l.log"
    p.write_text("\n".join(f"line{i}" for i in range(1000)) + "\n")
    out = lifecycle.tail_text(p, 3)
    assert out.splitlines() == ["line997", "line998", "line999"]
    assert lifecycle.tail_text(tmp_path / "missing.log", 3) == "(no log yet)"


# ---- healthy(): /v1/health first, legacy fallback only when absent (P4) --------


class _StubDaemon(http.server.BaseHTTPRequestHandler):
    """Configurable stand-in for whatever answers the port: subclasses
    set `health_body` (None = endpoint absent), `health_status`, and
    `root_body`."""

    health_body: ClassVar[bytes | None] = None
    health_status: ClassVar[int] = 200
    root_body: ClassVar[bytes] = b"not voco"

    def do_GET(self):  # BaseHTTPRequestHandler's casing contract
        if self.path == "/v1/health" and self.health_body is not None:
            body, status = self.health_body, self.health_status
        elif self.path == "/":
            body, status = self.root_body, 200
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def stub_server():
    servers: list[http.server.HTTPServer] = []

    def start(handler: type[_StubDaemon]) -> str:
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_healthy_accepts_the_real_health_endpoint(stub_server):
    class H(_StubDaemon):
        health_body: ClassVar[bytes | None] = b'{"service": "voco-d", "ok": true}'

    assert lifecycle.healthy(stub_server(H)) is True


def test_healthy_wrong_service_is_a_hard_no(stub_server):
    # a DIFFERENT service answering /v1/health must not fall back to
    # the body heuristic — even if its root page happens to say "voco"
    class H(_StubDaemon):
        health_body: ClassVar[bytes | None] = b'{"service": "imposter-d"}'
        root_body: ClassVar[bytes] = b"<html>my voco fanpage</html>"

    assert lifecycle.healthy(stub_server(H)) is False


def test_healthy_garbage_health_json_is_a_hard_no(stub_server):
    # a real voco-d never sends malformed health JSON, so a 200 full of
    # garbage is a squatter — no fallback (xai P4 round)
    class H(_StubDaemon):
        health_body: ClassVar[bytes | None] = b"<html>totally voco</html>"
        root_body: ClassVar[bytes] = b"<html>totally voco</html>"

    assert lifecycle.healthy(stub_server(H)) is False


def test_healthy_erroring_health_endpoint_is_a_hard_no(stub_server):
    # the endpoint EXISTS but 500s: something is there and it is not a
    # healthy voco-d — the voco-looking root page must not rescue it
    class H(_StubDaemon):
        health_body: ClassVar[bytes | None] = b"boom"
        health_status: ClassVar[int] = 500
        root_body: ClassVar[bytes] = b"<html>voco-ish</html>"

    assert lifecycle.healthy(stub_server(H)) is False


def test_healthy_endpoint_absent_falls_back_to_signature(stub_server):
    # pre-P4 daemon mid-upgrade: no /v1/health, voco signature on /
    class H(_StubDaemon):
        health_body = None
        root_body: ClassVar[bytes] = b"<html><title>voco deck</title></html>"

    assert lifecycle.healthy(stub_server(H)) is True


def test_healthy_random_listener_is_not_a_daemon(stub_server):
    class H(_StubDaemon):
        health_body = None
        root_body: ClassVar[bytes] = b"<html>welcome to nginx</html>"

    assert lifecycle.healthy(stub_server(H)) is False


# ---- follow_lines: `voco logs -f` survives rotation (P4) -----------------------

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="voco logs is POSIX-gated (st_ino identity)"
)


def _scripted_sleep(steps):
    """A sleep stand-in that advances the log file's life instead of
    waiting; running out of steps means the follower stalled."""
    it = iter(steps)

    def fake_sleep(_secs: float) -> None:
        step = next(it, None)
        assert step is not None, "follower slept more than the script allows"
        step()

    return fake_sleep


@posix_only
def test_follow_lines_survives_rotation(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("history\n")  # pre-existing content is not replayed

    def append_live():
        with log.open("a") as f:
            f.write("live1\n")

    def rotate_away():  # RotatingFileHandler renames on rollover
        log.rename(tmp_path / "daemon.log.1")

    def new_file_appears():
        log.write_text("fresh1\n")

    gen = lifecycle.follow_lines(
        log, sleep=_scripted_sleep([append_live, rotate_away, new_file_appears])
    )
    try:
        assert next(gen) == "live1\n"
        # rotation: old file renamed (a missing-file gap), new file
        # created — the follower reopens and reads the NEW file's start
        assert next(gen) == "fresh1\n"
    finally:
        gen.close()


@posix_only
def test_follow_lines_detects_truncation(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("a long line of history\n")

    def truncate_in_place():  # same inode, size below our position
        with log.open("w") as f:
            f.write("z\n")

    gen = lifecycle.follow_lines(log, sleep=_scripted_sleep([truncate_in_place]))
    try:
        assert next(gen) == "z\n"
    finally:
        gen.close()


@posix_only
def test_follow_lines_waits_for_a_missing_file(tmp_path):
    # first boot: no daemon.log yet — the follower waits for its birth
    # and then reads from the FIRST line (nothing to tail-skip)
    log = tmp_path / "daemon.log"

    def file_is_born():
        log.write_text("first line ever\n")

    gen = lifecycle.follow_lines(log, sleep=_scripted_sleep([file_is_born]))
    try:
        assert next(gen) == "first line ever\n"
    finally:
        gen.close()


# ---- service env propagation (xai P4 round) ------------------------------------


def test_service_env_carries_custom_state_dir(monkeypatch, tmp_path):
    # the plist's daemon.out path is computed from $VOCO_STATE_DIR, so
    # the spawned daemon must resolve the SAME state dir for daemon.log
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    parsed = plistlib.loads(lifecycle.build_launchd_plist(["voco-d"], tmp_path / "o"))
    assert parsed["EnvironmentVariables"] == {
        "VOCO_LOG_CONSOLE": "0",
        "VOCO_STATE_DIR": str(tmp_path / "sd"),
    }
    unit = lifecycle.systemd_unit(["voco-d"], tmp_path / "o")
    assert f"VOCO_STATE_DIR={tmp_path / 'sd'}" in unit
