"""voco doctor (BUILD-PROD P5): every check function with its edges
injected — no real daemon, microphone, or 300MB model required."""

from __future__ import annotations

import hashlib
import http.server
import os
import threading
from typing import ClassVar

import numpy as np
import pytest

from voco import assets
from voco_cli import doctor, lifecycle

# ---- daemon_row: /v1/health with squatter and pre-P4 discrimination -------------


class _Stub(http.server.BaseHTTPRequestHandler):
    health_body: ClassVar[bytes | None] = None
    root_body: ClassVar[bytes] = b"nope"

    def do_GET(self):
        if self.path == "/v1/health" and self.health_body is not None:
            body = self.health_body
        elif self.path == "/":
            body = self.root_body
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_server():
    servers: list[http.server.HTTPServer] = []

    def start(handler: type[_Stub]) -> str:
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_daemon_row_reads_live_facts(stub_server):
    class H(_Stub):
        health_body: ClassVar[bytes | None] = (
            b'{"service": "voco-d", "ok": true, "version": "1.2",'
            b' "uptime_s": 7.0, "voice": true, "floor_managed": true,'
            b' "floor_restarts": 2}'
        )

    row, health = doctor.daemon_row(stub_server(H))
    assert row.status == "ok"
    assert "v1.2" in row.detail and "voice on" in row.detail
    assert "2 floor restart(s)" in row.detail
    assert health["voice"] is True


def test_daemon_row_squatter_is_a_failure(stub_server):
    class H(_Stub):
        health_body: ClassVar[bytes | None] = b'{"service": "grafana"}'

    row, health = doctor.daemon_row(stub_server(H))
    assert row.status == "FAIL" and "another service" in row.detail
    assert health == {}


def test_daemon_row_pre_p4_voco_warns_to_restart(stub_server):
    class H(_Stub):  # no /v1/health, voco signature on /
        health_body = None
        root_body: ClassVar[bytes] = b"<title>voco deck</title>"

    row, _ = doctor.daemon_row(stub_server(H))
    assert row.status == "warn" and "restart" in row.detail


def test_daemon_row_nothing_listening_is_a_failure():
    row, _ = doctor.daemon_row("http://127.0.0.1:1")  # port 1: nothing
    assert row.status == "FAIL" and "voco up" in row.detail


def test_daemon_row_hostile_payload_types_fail_not_crash(stub_server):
    # unauthenticated endpoint = untrusted input: a fake voco-d sending
    # a string uptime must become a row, never a doctor traceback
    class H(_Stub):
        health_body: ClassVar[bytes | None] = (
            b'{"service": "voco-d", "ok": true, "uptime_s": "lol"}'
        )

    row, health = doctor.daemon_row(stub_server(H))
    assert row.status == "FAIL" and "malformed" in row.detail
    assert health == {}


def test_daemon_row_ok_false_is_a_failure(stub_server):
    class H(_Stub):
        health_body: ClassVar[bytes | None] = (
            b'{"service": "voco-d", "ok": false, "uptime_s": 1}'
        )

    row, _ = doctor.daemon_row(stub_server(H))
    assert row.status == "FAIL" and "ok=false" in row.detail


# ---- state_rows -------------------------------------------------------------------


def test_state_rows_fresh_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    rows = {r.name: r for r in doctor.state_rows(None)}  # no daemon to ask
    assert rows["state dir"].status == "ok"
    assert rows["pidfile"].status == "--"
    assert rows["daemon.log"].status == "--"
    assert "state split" not in rows  # unknowable without the config


def test_state_rows_flags_stale_pidfile_readonly(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path))
    pf = lifecycle.pidfile_path()
    pf.parent.mkdir(parents=True, exist_ok=True)
    # our own pid: alive but NOT voco-d → stale, and doctor must not heal
    import os

    pf.write_text(str(os.getpid()))
    rows = {r.name: r for r in doctor.state_rows(None)}
    assert rows["pidfile"].status == "warn"
    assert "pid reuse" in rows["pidfile"].detail
    assert pf.exists()  # read-only: the pidfile survives doctor


def test_state_rows_warns_on_diverged_state_dirs(monkeypatch, tmp_path):
    # P4 live-drill lesson: $VOCO_STATE_DIR does not move the registry
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "lifecycle"))
    cfg = {"state": {"dir": str(tmp_path / "elsewhere")}}
    rows = {r.name: r for r in doctor.state_rows(cfg)}
    assert rows["state split"].status == "warn"


def test_state_rows_default_registry_split_is_caught(monkeypatch, tmp_path):
    # THE P4 incident shape: $VOCO_STATE_DIR set, [state].dir absent —
    # lifecycle files move while the registry stays at the daemon default
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "lifecycle"))
    rows = {r.name: r for r in doctor.state_rows({})}
    assert rows["state split"].status == "warn"
    assert ".local/state/voco" in rows["state split"].detail


def test_state_rows_no_split_when_dirs_coincide(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    cfg = {"state": {"dir": str(tmp_path / "sd")}}
    assert "state split" not in {r.name for r in doctor.state_rows(cfg)}


@pytest.mark.skipif(os.name != "posix", reason="symlink plant needs POSIX")
def test_state_probe_never_writes_through_a_planted_symlink(monkeypatch, tmp_path):
    import os as _os

    sd = tmp_path / "sd"
    sd.mkdir()
    monkeypatch.setenv("VOCO_STATE_DIR", str(sd))
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (sd / f".doctor-probe.{_os.getpid()}").symlink_to(victim)
    rows = {r.name: r for r in doctor.state_rows(None)}
    assert victim.read_text() == "precious"  # never truncated
    assert rows["state dir"].status == "ok"  # plant removed, probe done


# ---- asset_rows -------------------------------------------------------------------


def test_asset_rows_missing_is_informational(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    rows = doctor.asset_rows(deep=False, voice_live=False)
    assert all(r.status == "--" for r in rows)
    assert all("downloads on first need" in r.detail for r in rows)


def test_asset_rows_small_file_always_hash_verified(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    d = assets.models_dir()
    d.mkdir(parents=True)
    good = b"the real silero bytes"
    monkeypatch.setattr(
        assets,
        "SILERO_VAD",
        assets.Asset(
            name="silero_vad.onnx",
            url="file:///x",
            sha256=hashlib.sha256(good).hexdigest(),
        ),
    )
    (d / "silero_vad.onnx").write_bytes(good)
    rows = {r.name: r for r in doctor.asset_rows(deep=False, voice_live=False)}
    assert rows["silero_vad.onnx"].status == "ok"
    assert "hash verified" in rows["silero_vad.onnx"].detail


def test_asset_rows_corrupt_cache_is_a_failure_with_the_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    d = assets.models_dir()
    d.mkdir(parents=True)
    (d / assets.SILERO_VAD.name).write_bytes(b"corrupted bytes")  # small → hashed
    rows = {r.name: r for r in doctor.asset_rows(deep=False, voice_live=False)}
    row = rows[assets.SILERO_VAD.name]
    assert row.status == "FAIL"
    # actionable: the exact (shell-safe) delete command
    assert f"rm {d / assets.SILERO_VAD.name}" in row.detail


def test_asset_rows_big_file_hashes_only_with_deep(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    monkeypatch.setattr(doctor, "DEEP_HASH_FREE_BYTES", 4)  # make 15 bytes "big"
    d = assets.models_dir()
    d.mkdir(parents=True)
    (d / assets.KOKORO_MODEL.name).write_bytes(b"not-the-model!!")
    shallow = {r.name: r for r in doctor.asset_rows(deep=False, voice_live=False)}
    assert shallow[assets.KOKORO_MODEL.name].status == "ok"
    assert "--deep verifies bytes" in shallow[assets.KOKORO_MODEL.name].detail
    deep = {r.name: r for r in doctor.asset_rows(deep=True, voice_live=False)}
    assert deep[assets.KOKORO_MODEL.name].status == "FAIL"


def test_asset_rows_live_voice_wins_over_cache_talk(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    rows = doctor.asset_rows(deep=False, voice_live=True)
    assert rows[0].name == "vad model" and rows[0].status == "ok"


def test_asset_rows_directory_squatting_the_name_is_a_row_not_a_crash(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path))
    d = assets.models_dir()
    d.mkdir(parents=True)
    (d / assets.SILERO_VAD.name).mkdir()  # a DIRECTORY where the model goes
    rows = {r.name: r for r in doctor.asset_rows(deep=True, voice_live=False)}
    row = rows[assets.SILERO_VAD.name]
    assert row.status == "FAIL" and "not a regular file" in row.detail


def test_asset_rows_remediation_is_shell_safe(monkeypatch, tmp_path):
    # a quote in $VOCO_CACHE must not make the printed rm unsafe to paste
    weird = tmp_path / "we'ird"
    monkeypatch.setenv("VOCO_CACHE", str(weird))
    d = assets.models_dir()
    d.mkdir(parents=True)
    (d / assets.SILERO_VAD.name).write_bytes(b"corrupt")
    rows = {r.name: r for r in doctor.asset_rows(deep=False, voice_live=False)}
    import shlex

    expected = shlex.quote(str(d / assets.SILERO_VAD.name))
    assert f"rm {expected}" in rows[assets.SILERO_VAD.name].detail


# ---- microphone + devices (injected sounddevice) ----------------------------------


class FakeSd:
    """Just enough of sounddevice for the doctor probes."""

    def __init__(self, samples: np.ndarray, devices=(0, 1)):
        self._samples = samples

        class _Default:
            device = devices

        self.default = _Default()

    def rec(self, frames, samplerate, channels, dtype):
        return self._samples

    def wait(self):
        pass

    def query_devices(self, idx):
        return {"name": f"Fake Device {idx}"}


def test_mic_row_live_audio_is_ok():
    noisy = np.array([[0], [3], [0]], dtype=np.int16)
    row = doctor.mic_row(sd_module=lambda: FakeSd(noisy))
    assert row.status == "ok"


def test_mic_row_pure_silence_names_the_permission():
    # macOS denies the mic with zeros, not an error — the honest signal
    silent = np.zeros((100, 1), dtype=np.int16)
    row = doctor.mic_row(sd_module=lambda: FakeSd(silent))
    assert row.status == "warn"
    assert "Microphone" in row.detail  # points at the macOS setting


def test_mic_row_capture_error_is_a_warning_not_a_crash():
    def broken():
        raise RuntimeError("PortAudio exploded")

    row = doctor.mic_row(sd_module=broken)
    assert row.status == "warn" and "PortAudio exploded" in row.detail


def test_device_rows_report_defaults():
    rows = doctor.device_rows(sd_module=lambda: FakeSd(np.zeros(1)))
    assert [r.status for r in rows] == ["ok", "ok"]
    assert "Fake Device 0" in rows[0].detail


def test_device_rows_missing_input_is_actionable():
    rows = doctor.device_rows(sd_module=lambda: FakeSd(np.zeros(1), devices=(-1, 1)))
    by_name = {r.name: r for r in rows}
    assert by_name["input device"].status == "warn"
    assert "capture nothing" in by_name["input device"].detail


# ---- Input Monitoring -------------------------------------------------------------


def test_input_monitoring_row_tristate():
    assert doctor.input_monitoring_row(lambda: True).status == "ok"
    denied = doctor.input_monitoring_row(lambda: False)
    assert denied.status == "warn" and "Input Monitoring" in denied.detail
    assert doctor.input_monitoring_row(lambda: None).status == "--"
