"""voco.assets — pinned model assets that survive leaving the repo
(BUILD-PROD P2).

Every model voco owns downloads on first need into the cache
($VOCO_CACHE or ~/.cache/voco/models), streamed to a .part file,
sha256-verified against a PIN, and renamed atomically — a torn or
tampered download can never be loaded. Explicitly configured paths
always win; relative configured paths resolve against the CONFIG
FILE's directory (never the cwd — that was the "works only from the
repo root" bug); a configured path that does not exist is an error,
because explicit config must not silently fall back.

Pins (verified 2026-07-09 against the exact bytes the daemon has been
running since 2026-07-03):
- silero VAD: snakers4/silero-vad @ commit b163605 (master line the
  local tuning was validated on; release v5.1.2 ships DIFFERENT bytes)
- kokoro model + voices: thewh1teagle/kokoro-onnx model-files-v1.0
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_SILERO_COMMIT = "b163605b3f44c3aadf28f97b125a2f7c461e9a7f"
_KOKORO_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)


class AssetError(RuntimeError):
    """Loud, actionable asset failure (missing config path, bad hash,
    offline first run)."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str


SILERO_VAD = Asset(
    name="silero_vad.onnx",
    url=(
        f"https://github.com/snakers4/silero-vad/raw/{_SILERO_COMMIT}"
        "/src/silero_vad/data/silero_vad.onnx"
    ),
    sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
)
KOKORO_MODEL = Asset(
    name="kokoro-v1.0.onnx",
    url=f"{_KOKORO_RELEASE}/kokoro-v1.0.onnx",
    sha256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
)
KOKORO_VOICES = Asset(
    name="voices-v1.0.bin",
    url=f"{_KOKORO_RELEASE}/voices-v1.0.bin",
    sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
)


def cache_dir() -> Path:
    env = os.environ.get("VOCO_CACHE")
    return Path(env) if env else Path.home() / ".cache" / "voco"


def models_dir() -> Path:
    return cache_dir() / "models"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(asset: Asset, dest_dir: Path | None = None, log=print) -> Path:
    """The asset's local path, downloading + verifying on first need.
    An EXISTING file is trusted without re-hashing (kokoro is 300MB —
    `voco doctor` owns deep verification).

    Concurrency-safe by construction (xai P2 blocker): every process
    writes its OWN temp file (.part.<pid> — never a shared inode a
    peer could keep mutating after our rename), fsyncs it, verifies
    the BYTES ON DISK against the pin, and only then atomically
    replaces dest. Two racing daemons each publish a fully-verified
    file; last write wins and both are identical."""
    dest = (dest_dir or models_dir()) / asset.name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    log(f"voco: downloading {asset.name} ...")
    h = hashlib.sha256()
    try:
        try:
            with (
                urllib.request.urlopen(asset.url, timeout=60) as resp,
                open(tmp, "wb") as out,
            ):
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                next_mark = 0.2
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total and done / total >= next_mark:
                        log(f"voco:   {asset.name}: {done * 100 // total}%")
                        next_mark += 0.2
                out.flush()
                os.fsync(out.fileno())  # durable before we ever publish
        except OSError as e:
            raise AssetError(
                f"could not download {asset.name} ({e}) — offline? Fetch it"
                f" yourself from {asset.url} into {dest.parent}"
            ) from e
        if h.hexdigest() != asset.sha256:
            raise AssetError(
                f"{asset.name} download failed verification (sha256"
                f" {h.hexdigest()[:12]}... != pinned {asset.sha256[:12]}...)"
                " — refused; retry, and report if it persists"
            )
        # verify what we PUBLISH, not just what we streamed: the disk
        # bytes are the ones a model loader will read
        if sha256_of(tmp) != asset.sha256:
            raise AssetError(f"{asset.name} was torn on disk after download — refused")
        try:
            tmp.replace(dest)  # atomic publish of verified bytes
        except OSError as e:
            raise AssetError(f"could not install {asset.name}: {e}") from e
    except AssetError:
        tmp.unlink(missing_ok=True)
        raise
    log(f"voco: {asset.name} ready ({dest})")
    return dest


def resolve_configured(value: str, config_dir: Path | None) -> Path:
    """A user-configured asset path: absolute stays; RELATIVE resolves
    against the config file's directory (predictable from any cwd)."""
    p = Path(value).expanduser()
    if not p.is_absolute() and config_dir is not None:
        p = config_dir / p
    return p


def ensure_silero(
    configured: str | None, config_dir: Path | None = None, log=print
) -> Path:
    """The VAD model path. Configured wins (and MUST exist — explicit
    config never silently falls back); default downloads the pin."""
    if configured:
        p = resolve_configured(configured, config_dir)
        if p.exists():
            return p
        raise AssetError(
            f"configured audio.silero_model not found: {p}"
            " (relative paths resolve against the config file's directory)"
        )
    return fetch(SILERO_VAD, log=log)


def ensure_kokoro(dest_dir: Path | None = None, log=print) -> tuple[Path, Path]:
    """The kokoro model + voices pair for the TTS floor."""
    return (
        fetch(KOKORO_MODEL, dest_dir=dest_dir, log=log),
        fetch(KOKORO_VOICES, dest_dir=dest_dir, log=log),
    )
