"""Workspace manifest persistence + single-writer lock (SPEC-WORKBENCH §8)."""

from __future__ import annotations

import json
import os

import pytest

from voco.adapters.manifest import WorkspaceLockError, WorkspaceManifest, safe_key
from voco.core.workspace import WorkspaceStore

LOCAL = {
    "host": "box",
    "cwd": "/r/proj",
    "repo": "proj",
    "branch": "main",
    "worktree": "/r/proj",
}


def test_dump_restore_round_trip_keeps_pages_and_findings():
    src = WorkspaceStore(now=lambda: 5.0)
    ws = src.resolve(LOCAL)
    src.push_doc(ws, name="notes", content="body")
    page = src.upsert_diff(
        ws,
        ref="branch:main",
        title="d",
        files=[{"path": "f"}],
        source={"branch": "main"},
    )
    src.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "f", "startLine": 1},
        text="c",
        blocking=True,
    )
    dumped = src.dump_workspace(ws)

    dst = WorkspaceStore(now=lambda: 9.0)
    restored = dst.restore_workspace(json.loads(json.dumps(dumped)))
    assert restored is not None
    assert restored.key == ws.key and restored.branch == "main"
    assert len(restored.pages) == 2
    (f,) = restored.findings.values()
    assert f.text == "c" and f.blocking
    # page id counter carried forward so new pages don't collide
    new = dst.push_doc(restored, name="more", content="x")
    assert new.page_id not in {p.page_id for p in ws.pages.values()}


def test_restore_skips_malformed_never_raises():
    dst = WorkspaceStore()
    assert dst.restore_workspace({"v": 999}) is None
    assert dst.restore_workspace({"v": 1}) is None  # missing keys
    assert dst.restore_workspace("nonsense") is None


def test_manifest_save_load_by_workspace(tmp_path):
    m = WorkspaceManifest(tmp_path)
    m.acquire()
    store = WorkspaceStore()
    ws = store.resolve(LOCAL)
    store.push_doc(ws, name="n", content="c")
    m.save(ws.key, store.dump_workspace(ws))
    # on disk, safe key path, 0600
    mpath = tmp_path / "workspaces" / safe_key(ws.key) / "manifest.json"
    assert mpath.exists()
    assert oct(mpath.stat().st_mode)[-3:] == "600"
    loaded, errors = m.load_all()
    assert errors == [] and len(loaded) == 1 and loaded[0]["key"] == ws.key
    m.release()


def test_lock_blocks_second_live_holder(tmp_path):
    a = WorkspaceManifest(tmp_path)
    a.acquire()
    # simulate a second daemon: same pid would be "self", so forge a foreign
    # live holder by writing a lock for THIS pid but pretending it's another.
    b = WorkspaceManifest(tmp_path)
    # our own pid is the holder -> acquire() treats hpid==getpid() as not-live
    # so a re-acquire by the same process succeeds (idempotent-ish).
    b.acquire()  # same pid, allowed
    # Now write a lock owned by a bogus-but-live pid (pid 1 always alive) with
    # a matching start nonce so it reads as live.
    from voco.adapters.manifest import _proc_start

    (tmp_path / "daemon.lock").write_text(
        json.dumps({"pid": 1, "start": _proc_start(1)}), encoding="utf-8"
    )
    c = WorkspaceManifest(tmp_path)
    with pytest.raises(WorkspaceLockError):
        c.acquire()


def test_lock_takes_over_dead_holder(tmp_path):
    # A lock from a dead pid (very high, unlikely alive) is taken over.
    dead = 4000000
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "daemon.lock").write_text(
        json.dumps({"pid": dead, "start": "x"}), encoding="utf-8"
    )
    m = WorkspaceManifest(tmp_path)
    m.acquire()  # should not raise
    held = json.loads((tmp_path / "daemon.lock").read_text())
    assert held["pid"] == os.getpid()
    m.release()
