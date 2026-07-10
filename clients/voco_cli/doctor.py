"""voco doctor — the environment diagnostic (BUILD-PROD P5).

One screen that answers "why doesn't voice work on this machine":
daemon reachability (via /v1/health, with squatter detection), state-dir
health (writability, pidfile sanity, lifecycle-vs-registry divergence),
pinned model cache (presence always, byte verification with --deep),
audio devices, a real microphone capture (macOS denies the mic with
SILENCE, not an error — all-zero capture is the honest signal), the
Input Monitoring grant PTT needs, and the service probes (TTS synth,
first mate). Warnings don't fail; only a dead required piece exits
non-zero. Doctor is read-mostly: its only writes are creating the
state dir if absent (exactly what voco up would create) and a
create-then-delete O_EXCL write probe inside it.

Boundary note: doctor imports PURE pieces of the daemon package — the
asset pins and the ctypes permission probe — because they are shared
constants/syscall wrappers shipped in the same wheel, and duplicating
64-char hashes here would rot. Daemon STATE still flows over HTTP only;
`lifecycle.py` keeps the strict no-import rule.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from voco import assets
from voco.adapters.hotkey import input_monitoring_granted
from voco_cli import lifecycle

MIC_PROBE_MS = 150
DEEP_HASH_FREE_BYTES = 10 << 20  # small files are always worth hashing


@dataclass(frozen=True)
class Row:
    status: str  # ok | warn | FAIL | --
    name: str
    detail: str

    @property
    def is_failure(self) -> bool:
        return self.status == "FAIL"


# ---- daemon (via the P4 health endpoint) ----------------------------------------


def daemon_row(base_url: str, timeout: float = 3.0) -> tuple[Row, dict]:
    """The daemon check + whatever live facts /v1/health returned.
    Distinguishes: healthy voco / pre-P4 voco (no endpoint yet) /
    another service squatting the port / nothing listening."""
    try:
        with urllib.request.urlopen(base_url + "/v1/health", timeout=timeout) as r:
            body = r.read(4096).decode(errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):  # endpoint absent: pre-P4 voco or a squatter
            if lifecycle.healthy(base_url, timeout=timeout):
                return (
                    Row(
                        "warn",
                        "daemon",
                        "up, but running pre-/v1/health code — restart it"
                        " (voco down && voco up) to finish the upgrade",
                    ),
                    {},
                )
            return (
                Row(
                    "FAIL",
                    "daemon",
                    f"another service answers {base_url} — voco-d cannot"
                    " start there; free the port or use --port",
                ),
                {},
            )
        return (
            Row("FAIL", "daemon", f"{base_url}/v1/health said HTTP {e.code}"),
            {},
        )
    except Exception as e:
        return (
            Row(
                "FAIL",
                "daemon",
                f"unreachable ({getattr(e, 'reason', e)}) — start it: voco up",
            ),
            {},
        )
    try:
        info = json.loads(body)
    except ValueError:
        info = None
    if not isinstance(info, dict) or info.get("service") != "voco-d":
        return (
            Row(
                "FAIL",
                "daemon",
                f"another service answers {base_url} — voco-d cannot"
                " start there; free the port or use --port",
            ),
            {},
        )
    # the payload is untrusted input from an unauthenticated endpoint:
    # validate the shape instead of formatting whatever arrived (a bad
    # field must become a row, never a doctor traceback)
    up_s = info.get("uptime_s", 0)
    restarts = info.get("floor_restarts", 0)
    if not isinstance(up_s, int | float) or not isinstance(restarts, int):
        return (
            Row(
                "FAIL",
                "daemon",
                f"{base_url}/v1/health answers as voco-d but with a"
                " malformed payload — a squatter faking the signature,"
                " or a seriously broken daemon",
            ),
            {},
        )
    if info.get("ok") is not True:  # the daemon itself says unhealthy
        return (
            Row("FAIL", "daemon", "up but reporting ok=false — check voco logs"),
            info,
        )
    detail = (
        f"up (v{info.get('version', '?')}, uptime {up_s:.0f}s,"
        f" voice {'on' if info.get('voice') else 'OFF'},"
        f" floor {'managed' if info.get('floor_managed') else 'unmanaged'}"
    )
    if restarts:
        detail += f", {restarts} floor restart(s)"
    return Row("ok", "daemon", detail + ")"), info


# ---- state-dir health ------------------------------------------------------------


def state_rows(cfg: dict | None) -> list[Row]:
    """cfg is the daemon-resolved config, or None when no daemon was
    reachable to ask (the [state].dir comparison is skipped then)."""
    rows: list[Row] = []
    sd = lifecycle.state_dir()
    try:
        # mkdir is the one directory doctor may create: it is exactly
        # what voco up would create, and writability of a fresh install
        # can't be probed otherwise
        sd.mkdir(parents=True, exist_ok=True)
        # O_EXCL|O_NOFOLLOW + a per-process name: a planted symlink or
        # leftover must never be written through (P4/P5 xai class)
        probe = sd / f".doctor-probe.{os.getpid()}"
        probe.unlink(missing_ok=True)
        fd = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(fd)
        probe.unlink()
        rows.append(Row("ok", "state dir", f"{sd} (writable)"))
    except OSError as e:
        rows.append(
            Row(
                "FAIL",
                "state dir",
                f"{sd} not writable ({e}) — voco up/logs and the daemon's"
                " own log cannot work until this is fixed",
            )
        )
        return rows  # everything below lives in this dir

    # pidfile sanity — read-only (doctor reports, it never heals) and
    # identity-aware (a recycled pid must not read as a live daemon)
    pid = lifecycle.read_pidfile(lifecycle.pidfile_path())
    if pid is None:
        rows.append(Row("--", "pidfile", "no managed daemon recorded"))
    else:
        cmd = lifecycle.pid_cmdline(pid)
        if cmd and lifecycle.looks_like_voco(cmd):
            rows.append(Row("ok", "pidfile", f"managed daemon pid {pid}"))
        elif cmd == "":
            rows.append(Row("warn", "pidfile", f"pid {pid} unverifiable (ps failed)"))
        else:
            what = "not running" if cmd is None else "not voco-d (pid reuse)"
            rows.append(
                Row(
                    "warn",
                    "pidfile",
                    f"stale — pid {pid} is {what}; voco up will clean this",
                )
            )

    log = lifecycle.log_path()
    if log.exists():
        rows.append(Row("ok", "daemon.log", f"{log.stat().st_size:,} bytes"))
    else:
        rows.append(Row("--", "daemon.log", "not written yet (voco up creates it)"))

    # lifecycle files vs the session registry (config [state].dir): they
    # coincide by default; divergence is legal but worth a look (P4
    # live-drill lesson — $VOCO_STATE_DIR does NOT move the registry,
    # so a set $VOCO_STATE_DIR with an unset [state].dir IS a split).
    # Compared LITERALLY, exactly as the daemon builds its StateStore
    # path (daemon.py does no expanduser/resolve on [state].dir).
    if cfg is not None:
        registry_dir = Path(
            cfg.get("state", {}).get("dir")
            # the daemon's built-in default (daemon.py StateStore ctor)
            or Path.home() / ".local" / "state" / "voco"
        )
        if registry_dir != sd:
            rows.append(
                Row(
                    "warn",
                    "state split",
                    f"lifecycle files in {sd} but the session registry in"
                    f" {registry_dir} ([state].dir) — voco logs/pidfile and"
                    " daemon state live apart; intended?",
                )
            )
    return rows


# ---- pinned model cache ----------------------------------------------------------


def asset_rows(deep: bool, voice_live: bool) -> list[Row]:
    """Presence for every pinned asset; byte verification when --deep
    (small files are always hashed — P2 deferred the deep verify here)."""
    rows: list[Row] = []
    if voice_live:
        rows.append(Row("ok", "vad model", "loaded by the running daemon"))
    for asset in (assets.SILERO_VAD, assets.KOKORO_MODEL, assets.KOKORO_VOICES):
        path = assets.models_dir() / asset.name
        # a hostile or broken cache is what doctor diagnoses — it must
        # produce rows, never a traceback (dir squatting the name,
        # unreadable file, entry vanishing mid-check)
        try:
            if not path.exists():
                rows.append(
                    Row("--", asset.name, "not cached (downloads on first need)")
                )
                continue
            if not path.is_file():
                rows.append(
                    Row(
                        "FAIL",
                        asset.name,
                        f"{path} is not a regular file — remove it so the"
                        " pinned download can take its place",
                    )
                )
                continue
            size = path.stat().st_size
            if deep or size <= DEEP_HASH_FREE_BYTES:
                verified = assets.sha256_of(path) == asset.sha256
            else:
                rows.append(
                    Row(
                        "ok",
                        asset.name,
                        f"cached, {size:,} bytes (voco doctor --deep verifies bytes)",
                    )
                )
                continue
        except OSError as e:
            rows.append(Row("FAIL", asset.name, f"cache entry unreadable ({e})"))
            continue
        if verified:
            rows.append(Row("ok", asset.name, f"cached, hash verified ({path})"))
        else:
            rows.append(
                Row(
                    "FAIL",
                    asset.name,
                    f"HASH MISMATCH — corrupt or tampered; delete it to"
                    f" re-download: rm {shlex.quote(str(path))}",
                )
            )
    return rows


# ---- audio devices + microphone ---------------------------------------------------


def _sd():  # tiny seam: sounddevice import, injectable in tests
    import sounddevice

    return sounddevice


def device_rows(sd_module: Callable[[], object] = _sd) -> list[Row]:
    try:
        sd = sd_module()
    except Exception as e:
        return [Row("warn", "audio", f"sounddevice unavailable ({e})")]
    rows: list[Row] = []
    for kind, idx_key in (("input", 0), ("output", 1)):
        try:
            default = sd.default.device[idx_key]  # type: ignore[attr-defined]
            if default is None or default < 0:
                raise LookupError("no default device")
            name = sd.query_devices(default)["name"]  # type: ignore[attr-defined]
            rows.append(Row("ok", f"{kind} device", name))
        except Exception:
            why = "VAD and PTT capture nothing" if kind == "input" else "voco is mute"
            rows.append(Row("warn", f"{kind} device", f"no default {kind} — {why}"))
    return rows


def mic_row(sd_module: Callable[[], object] = _sd) -> Row:
    """Capture a beat of real audio. macOS denies the microphone with
    SILENCE (all zeros), not an error — so pure silence is the signal.
    Running this may trigger the OS permission prompt; doctor is the
    right place for that to happen (the user is watching)."""
    try:
        sd = sd_module()
        frames = int(16000 * MIC_PROBE_MS / 1000)
        buf = sd.rec(  # type: ignore[attr-defined]
            frames, samplerate=16000, channels=1, dtype="int16"
        )
        sd.wait()  # type: ignore[attr-defined]
        if bool((buf == 0).all()):
            return Row(
                "warn",
                "microphone",
                f"{MIC_PROBE_MS}ms of PURE silence — mic permission denied"
                " (System Settings → Privacy & Security → Microphone) or a"
                " dead input device",
            )
        return Row("ok", "microphone", f"captured {MIC_PROBE_MS}ms of live audio")
    except Exception as e:
        return Row("warn", "microphone", f"cannot capture ({e})")


def input_monitoring_row(
    probe: Callable[[], bool | None] = input_monitoring_granted,
) -> Row:
    granted = probe()
    if granted is True:
        return Row("ok", "input monitor", "granted — PTT can see the hotkey")
    if granted is False:
        return Row(
            "warn",
            "input monitor",
            "NOT granted — PTT is silent until you grant Input Monitoring"
            " (System Settings → Privacy & Security → Input Monitoring)"
            " and restart voco-d",
        )
    return Row("--", "input monitor", "cannot tell on this platform")


# ---- service probes (moved from main.py, unchanged behavior) ----------------------


def probe_tts(tts_cfg: dict) -> str | None:
    """POST a real tiny synth and require audio bytes back — a random
    HTTP listener squatting the port (OrbStack does) must not read ok."""
    url = f"{tts_cfg.get('base_url', 'http://127.0.0.1:8880/v1').rstrip('/')}"
    body = json.dumps(
        {
            "model": tts_cfg.get("model", "kokoro"),
            "voice": tts_cfg.get("voice", "af_heart"),
            "input": "hi",
            "response_format": "pcm",
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{url}/audio/speech",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(4096)
        return None if len(data) >= 1000 else "answers but returns no audio"
    except Exception as e:
        return str(getattr(e, "reason", e))


def probe_mate(base: str) -> str | None:
    """GET /models and require OpenAI-shaped JSON back."""
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/models", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            obj = json.loads(resp.read(65536).decode())
        if isinstance(obj, dict) and ("data" in obj or "object" in obj):
            return None
        return "answers but doesn't look OpenAI-compatible"
    except Exception as e:
        return str(getattr(e, "reason", e))


# ---- the command -----------------------------------------------------------------


def cmd_doctor(client, deep: bool = False) -> int:
    print(f"voco doctor — {client.base_url}")

    def show(row: Row) -> None:
        print(f"  {row.status:<4} {row.name:<14} {row.detail}")

    failures = 0

    def emit(*rows: Row) -> None:
        nonlocal failures
        for row in rows:
            show(row)
            failures += row.is_failure

    drow, health = daemon_row(client.base_url)
    emit(drow)

    # config rides the API so doctor sees what the DAEMON resolved —
    # any up daemon serves it, including a pre-P4 one (warn, not FAIL);
    # None means "nobody to ask", and cfg-dependent checks skip then
    cfg: dict | None = None
    if not drow.is_failure:
        try:
            cfg = client._request("POST", "/v1/control/config.get", {}, timeout=3)
        except Exception as e:
            emit(Row("warn", "config", f"config.get failed ({e}); probing defaults"))

    emit(*state_rows(cfg))
    emit(*asset_rows(deep, voice_live=bool(health.get("voice"))))
    emit(*device_rows())
    emit(mic_row())
    emit(input_monitoring_row())

    tts_cfg = (cfg or {}).get("tts", {})
    tts_url = tts_cfg.get("base_url", "http://127.0.0.1:8880/v1")
    err = probe_tts(tts_cfg)
    if err is None:
        emit(Row("ok", "tts", f"{tts_url} (synthesized a test phrase)"))
    else:
        emit(
            Row(
                "warn",
                "tts",
                f"{tts_url}: {err} — voice will be silent"
                " (start voco-tts-floor or mlx-audio)",
            )
        )

    mate_url = (cfg or {}).get("first_mate", {}).get("base_url")
    if mate_url:
        err = probe_mate(mate_url)
        if err is None:
            emit(Row("ok", "first_mate", mate_url))
        else:
            emit(
                Row(
                    "warn",
                    "first_mate",
                    f"{mate_url}: {err} — degraded mode (phrase table +"
                    " forward-verbatim)",
                )
            )
    else:
        emit(Row("--", "first_mate", "not configured (degraded mode by design)"))

    if shutil.which("tmux"):
        inside = (
            "this shell CAN inject"
            if os.environ.get("TMUX_PANE")
            else "run agents inside tmux to enable inject"
        )
        emit(Row("ok", "tmux", inside))
    else:
        emit(Row("warn", "tmux", "not installed — no managed sessions, no inject"))

    # listener script (voice_init output) — a stale MCP server keeps
    # writing the pre-rework streaming variant, which never exits and so
    # never wakes the agent (live-test find).
    script = assets.cache_dir() / "listen.sh"
    if script.exists():
        try:
            stale = "--stream" in script.read_text()
        except OSError:
            stale = False
        if stale:
            emit(
                Row(
                    "warn",
                    "listen.sh",
                    "stale streaming script — restart the agent's MCP server"
                    " (old voice_init), then call voice_init again",
                )
            )
        else:
            emit(Row("ok", "listen.sh", "one-shot listener script"))
    else:
        emit(Row("--", "listen.sh", "not written yet (voice_init creates it)"))

    for mod, extra, why in (
        ("faster_whisper", "stt", "speech-to-text"),
        ("sounddevice", "(core)", "mic/speaker"),
        ("pynput", "ptt", "push-to-talk hotkey"),
        ("openwakeword", "wake", "wake-word"),
        ("kokoro_onnx", "floor", "bundled TTS floor"),
    ):
        found = importlib.util.find_spec(mod) is not None
        emit(
            Row(
                "ok" if found else "--",
                mod,
                why if found else f"{why} — uv sync --extra {extra}",
            )
        )

    if failures:
        print(f"\n{failures} FAILURE(S) — see rows above")
        return 1
    print("\nall required pieces up")
    return 0
