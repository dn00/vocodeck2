"""Pinned model assets (BUILD-PROD P2): atomic verified downloads +
config-relative resolution."""

from __future__ import annotations

import hashlib
import http.client
import os
import urllib.error
import urllib.request
from typing import ClassVar

import pytest

from voco import assets


def _local_asset(tmp_path, content: bytes, pin: str | None = None):
    src = tmp_path / "src.bin"
    src.write_bytes(content)
    return assets.Asset(
        name="thing.bin",
        url=src.as_uri(),
        sha256=pin or hashlib.sha256(content).hexdigest(),
    )


def test_fetch_downloads_verifies_and_is_atomic(tmp_path):
    a = _local_asset(tmp_path, b"model-bytes" * 100)
    dest_dir = tmp_path / "cache"
    logs: list[str] = []
    p = assets.fetch(a, dest_dir=dest_dir, log=logs.append)
    assert p == dest_dir / "thing.bin"
    assert p.read_bytes() == b"model-bytes" * 100
    # per-process temp names (concurrency blocker fix) — none left over
    assert not list(dest_dir.glob("*.part*"))
    assert any("downloading" in line for line in logs)


def test_fetch_temp_is_per_process(tmp_path):
    # two daemons must never share a temp inode: the name embeds the pid
    import os

    a = _local_asset(tmp_path, b"x")
    dest = tmp_path / "cache" / a.name
    tmp = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    assert str(os.getpid()) in tmp.name


def test_fetch_refuses_bad_hash_and_leaves_nothing(tmp_path):
    a = _local_asset(tmp_path, b"payload", pin="0" * 64)
    dest_dir = tmp_path / "cache"
    with pytest.raises(assets.AssetError, match="verification"):
        assets.fetch(a, dest_dir=dest_dir, log=lambda *_: None)
    assert not (dest_dir / "thing.bin").exists()
    assert not list(dest_dir.glob("*.part*"))


def test_fetch_trusts_existing_file(tmp_path):
    dest_dir = tmp_path / "cache"
    dest_dir.mkdir()
    (dest_dir / "thing.bin").write_bytes(b"already-here")
    # a dead URL proves the network is never touched for existing files
    a = assets.Asset(name="thing.bin", url="file:///nonexistent", sha256="x" * 64)
    p = assets.fetch(a, dest_dir=dest_dir, log=lambda *_: None)
    assert p.read_bytes() == b"already-here"


def test_fetch_offline_is_actionable(tmp_path):
    a = assets.Asset(
        name="thing.bin",
        url=(tmp_path / "missing.bin").as_uri(),
        sha256="0" * 64,
    )
    with pytest.raises(assets.AssetError, match="Fetch it yourself"):
        assets.fetch(a, dest_dir=tmp_path / "cache", log=lambda *_: None)


# ---- P4 error taxonomy: each failure names itself and its fix ------------------


def _raising_urlopen(monkeypatch, exc: BaseException) -> None:
    def urlopen(url, timeout=None):
        raise exc

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_fetch_http_error_names_the_moved_url(tmp_path, monkeypatch):
    a = _local_asset(tmp_path, b"x")
    _raising_urlopen(
        monkeypatch, urllib.error.HTTPError(a.url, 404, "Not Found", None, None)
    )
    with pytest.raises(assets.AssetError, match=r"HTTP 404.*pinned URL may have moved"):
        assets.fetch(a, dest_dir=tmp_path / "cache", log=lambda *_: None)


def test_fetch_timeout_is_named(tmp_path, monkeypatch):
    a = _local_asset(tmp_path, b"x")
    _raising_urlopen(monkeypatch, TimeoutError("read timed out"))
    with pytest.raises(assets.AssetError, match="timed out"):
        assets.fetch(a, dest_dir=tmp_path / "cache", log=lambda *_: None)


def test_fetch_urlerror_wrapped_timeout_is_still_a_timeout(tmp_path, monkeypatch):
    # urllib wraps CONNECT timeouts as URLError(reason=timeout); the
    # taxonomy must not misname that "offline" (xai P4 round)
    a = _local_asset(tmp_path, b"x")
    _raising_urlopen(monkeypatch, urllib.error.URLError(TimeoutError("connect")))
    with pytest.raises(assets.AssetError, match="timed out"):
        assets.fetch(a, dest_dir=tmp_path / "cache", log=lambda *_: None)


def test_fetch_midstream_break_is_named(tmp_path, monkeypatch):
    class TornResponse:
        headers: ClassVar[dict] = {"Content-Length": "1000"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: TornResponse())
    a = _local_asset(tmp_path, b"x")
    with pytest.raises(assets.AssetError, match="broke mid-stream"):
        assets.fetch(a, dest_dir=tmp_path / "cache", log=lambda *_: None)
    assert not list((tmp_path / "cache").glob("*.part*"))  # temp cleaned


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permission bits and a non-root user",
)
def test_fetch_disk_write_failure_points_at_the_cache_dir(tmp_path):
    a = _local_asset(tmp_path, b"x")
    dest_dir = tmp_path / "cache"
    dest_dir.mkdir()
    dest_dir.chmod(0o500)  # readable, not writable
    try:
        with pytest.raises(assets.AssetError, match="could not write"):
            assets.fetch(a, dest_dir=dest_dir, log=lambda *_: None)
    finally:
        dest_dir.chmod(0o700)  # let tmp_path cleanup succeed


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permission bits and a non-root user",
)
def test_fetch_uncreatable_cache_dir_is_actionable(tmp_path):
    # first run on a machine where the cache ROOT can't be created —
    # must be a named AssetError, not a raw OSError traceback
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0o500)
    a = _local_asset(tmp_path, b"x")
    try:
        with pytest.raises(assets.AssetError, match="could not create the cache dir"):
            assets.fetch(a, dest_dir=root / "cache", log=lambda *_: None)
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.name != "posix", reason="symlink plant needs POSIX")
def test_fetch_never_publishes_through_a_planted_tmp(tmp_path):
    # a pre-existing .part.<pid> — crash debris under a recycled pid, or
    # a planted symlink — must not become the published inode
    content = b"real-model-bytes"
    a = _local_asset(tmp_path, content)
    dest_dir = tmp_path / "cache"
    dest_dir.mkdir()
    dest = dest_dir / a.name
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"peer-owned")
    planted = dest.with_suffix(dest.suffix + f".part.{os.getpid()}")
    planted.symlink_to(victim)
    p = assets.fetch(a, dest_dir=dest_dir, log=lambda *_: None)
    assert p.read_bytes() == content
    assert not p.is_symlink()  # published a REAL file, not the plant
    assert victim.read_bytes() == b"peer-owned"  # never wrote through it


def test_resolve_configured_relative_uses_config_dir(tmp_path):
    p = assets.resolve_configured("models/x.onnx", tmp_path / "cfgdir")
    assert p == tmp_path / "cfgdir" / "models" / "x.onnx"
    absolute = tmp_path / "abs.onnx"
    assert assets.resolve_configured(str(absolute), tmp_path / "cfgdir") == absolute


def test_ensure_silero_configured_must_exist(tmp_path):
    real = tmp_path / "vad.onnx"
    real.write_bytes(b"vad")
    assert assets.ensure_silero(str(real)) == real
    with pytest.raises(assets.AssetError, match="not found"):
        assets.ensure_silero("missing/vad.onnx", config_dir=tmp_path)


def test_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_CACHE", str(tmp_path / "vc"))
    assert assets.cache_dir() == tmp_path / "vc"
    assert assets.models_dir() == tmp_path / "vc" / "models"
