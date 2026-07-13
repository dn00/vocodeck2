"""W5 — re-review / inter-diff (ported from the oracle's
rereview.test.mjs: same three-rev scenario, same assertions).

- a re-push records `interdiff` on the diff page — per-file changed /
  added / removed / unchanged since the rev it replaced;
- export: a STALE diff finding's anchor carries `area_changed` — True
  when its file moved in the latest re-push (incl. dropped files),
  False when untouched — the agent's re-review signal;
- the legacy array stays byte-exact five-field (interdiff never leaks);
- the live-git tracker re-pushes only when content actually moves.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from voco.core.diff import parse_diff
from voco.core.interdiff import area_touched, compute_interdiff
from voco.core.review_export import export_workspace
from voco.core.workspace import WorkspaceStore
from voco.daemon import Daemon
from voco.server.workbench import diff_fingerprint

FOO_V1 = """diff --git a/lib/foo.js b/lib/foo.js
--- a/lib/foo.js
+++ b/lib/foo.js
@@ -1,3 +1,3 @@
 const a = 0;
-const x = 0;
+const x = 1;
 return x;
"""

BAR_HUNK = """diff --git a/lib/bar.js b/lib/bar.js
--- a/lib/bar.js
+++ b/lib/bar.js
@@ -1,2 +1,3 @@
 const b = 0;
+export const bar = 1;
 return b;
"""

DIFF_V2 = (
    """diff --git a/lib/foo.js b/lib/foo.js
--- a/lib/foo.js
+++ b/lib/foo.js
@@ -1,3 +1,3 @@
 const a = 0;
-const x = 0;
+const x = 2;
 return x;
"""
    + BAR_HUNK
)

DIFF_V3 = (
    """diff --git a/lib/foo.js b/lib/foo.js
--- a/lib/foo.js
+++ b/lib/foo.js
@@ -1,3 +1,3 @@
 const a = 0;
-const x = 0;
+const x = 3;
 return x;
"""
    + BAR_HUNK
)


def push(store, ws, text):
    return store.upsert_diff(
        ws,
        ref="diff:test",
        title="diff",
        files=parse_diff(text),
        source={"branch": "main"},
        diff_key=diff_fingerprint(text),
    )


@pytest.fixture
def ws_store():
    store = WorkspaceStore()
    ws = store.resolve({"host": "m", "worktree": "/repo"})
    return store, ws


# ---- interdiff on re-push (the oracle scenario) --------------------------------


def test_first_push_has_no_interdiff(ws_store):
    store, ws = ws_store
    page = push(store, ws, FOO_V1)
    assert page.rev == 1
    assert "interdiff" not in page.data


def test_repush_records_changed_added_removed_unchanged(ws_store):
    store, ws = ws_store
    push(store, ws, FOO_V1)
    page = push(store, ws, DIFF_V2)
    assert page.rev == 2
    inter = page.data["interdiff"]
    assert inter["since_rev"] == 1
    assert inter["changed"] == ["lib/foo.js"]
    assert inter["added"] == ["lib/bar.js"]
    assert inter["removed"] == []

    page = push(store, ws, DIFF_V3)
    assert page.rev == 3
    inter = page.data["interdiff"]
    assert inter["since_rev"] == 2
    assert inter["changed"] == ["lib/foo.js"]
    assert inter["unchanged"] == ["lib/bar.js"]


def test_removed_file_counts_as_touched():
    prev = parse_diff(DIFF_V2)
    nxt = parse_diff(FOO_V1)  # bar drops out of the diff
    inter = compute_interdiff(prev, nxt, 2)
    assert inter["removed"] == ["lib/bar.js"]
    assert area_touched(inter, "lib/bar.js") is True
    assert area_touched(inter, "lib/nope.js") is False
    assert area_touched(None, "lib/foo.js") is False


# ---- export: area_changed on stale anchors --------------------------------------


def test_export_stamps_area_changed_on_stale_findings(ws_store, tmp_path):
    store, ws = ws_store
    page = push(store, ws, FOO_V1)
    foo = store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "lib/foo.js", "side": "new", "startLine": 2, "endLine": 2},
        text="x should stay 0",
    )
    push(store, ws, DIFF_V2)
    bar = store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "lib/bar.js", "side": "new", "startLine": 2, "endLine": 2},
        text="name the export better",
        kind="nit",
    )
    push(store, ws, DIFF_V3)  # foo moves again; bar untouched

    result = export_workspace(
        store, ws.key, out=str(tmp_path / "notes.json"), data_dir=tmp_path
    )
    legacy = json.loads((tmp_path / "notes.json").read_text())
    assert sorted(legacy[0].keys()) == [
        "concern",
        "endLine",
        "file",
        "side",
        "startLine",
    ], "interdiff never leaks into the legacy array"

    anchors = {
        a["finding_id"]: a
        for a in json.loads((tmp_path / "notes.anchors.json").read_text())
    }
    assert anchors[foo.finding_id]["stale"] is True
    assert anchors[foo.finding_id]["area_changed"] is True  # moved: re-check
    assert anchors[bar.finding_id]["stale"] is True  # rev-2 finding at rev 3
    assert anchors[bar.finding_id]["area_changed"] is False  # still stands
    assert result["count"] == 2


def test_current_rev_findings_carry_no_area_changed(ws_store, tmp_path):
    store, ws = ws_store
    page = push(store, ws, FOO_V1)
    store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "lib/foo.js", "side": "new", "startLine": 2},
        text="fresh",
    )
    export_workspace(store, ws.key, out=str(tmp_path / "n.json"), data_dir=tmp_path)
    (anchor,) = json.loads((tmp_path / "n.anchors.json").read_text())
    assert anchor["stale"] is False
    assert "area_changed" not in anchor


# ---- live-git tracker -------------------------------------------------------------


class ScriptedResolver:
    """Stands in for DiffResolver: returns whatever text is loaded."""

    def __init__(self) -> None:
        self.text: str | None = FOO_V1
        self.calls = 0

    def resolve(self, source, root) -> str:
        self.calls += 1
        if self.text is None:
            raise RuntimeError("transient git state")
        return self.text


@pytest.fixture
def live_daemon():
    d = Daemon({}, no_audio=True)
    d.bridge.diff_resolver = ScriptedResolver()
    return d


async def test_live_refresh_bumps_only_on_content_change(live_daemon):
    d = live_daemon
    ws = d.workspaces.resolve({"host": "m", "worktree": "/repo"})
    page = push(d.workspaces, ws, FOO_V1)

    await d._live_refresh(ws, page)  # same content: no bump
    assert page.rev == 1

    d.bridge.diff_resolver.text = DIFF_V2
    await d._live_refresh(ws, page)
    assert page.rev == 2
    assert page.data["interdiff"]["added"] == ["lib/bar.js"]


async def test_live_refresh_skips_transient_and_empty_states(live_daemon):
    d = live_daemon
    ws = d.workspaces.resolve({"host": "m", "worktree": "/repo"})
    page = push(d.workspaces, ws, FOO_V1)

    d.bridge.diff_resolver.text = None  # resolver raises (rebase, lock)
    await d._live_refresh(ws, page)
    assert page.rev == 1

    d.bridge.diff_resolver.text = "not a diff at all"  # parses to no files
    await d._live_refresh(ws, page)
    assert page.rev == 1  # conservative: never clobber with empty


async def test_live_git_tick_bounds_workspace_concurrency(monkeypatch):
    d = Daemon({"workbench": {"live_git_concurrency": 2}}, no_audio=True)
    for n in range(5):
        d.workspaces.resolve({"host": "m", "worktree": f"/repo/{n}"})
    active = 0
    peak = 0

    async def blocking(_fn):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"branch": "main", "dirty": False}

    monkeypatch.setattr(d, "_run_blocking", blocking)
    await d._live_git_tick("m", lambda _root: {})
    assert peak == 2
    assert all(ws.git is not None for ws in d.workspaces.all())


async def test_workspace_live_toggle(live_daemon):
    d = live_daemon
    ws = d.workspaces.resolve({"host": "m", "worktree": "/repo"})
    result = await d._control("workspace.live", {"workspace": ws.key, "live": False})
    assert result == {"workspace": ws.key, "live": False}
    assert d._live_workspaces[ws.key] is False
    with pytest.raises(ValueError, match="unknown workspace"):
        await d._control("workspace.live", {"workspace": "nope"})
