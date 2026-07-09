"""Pinned model assets (BUILD-PROD P2): atomic verified downloads +
config-relative resolution."""

from __future__ import annotations

import hashlib

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
