"""Findings ledger + review export (SPEC-WORKBENCH §4, §3.3)."""

from __future__ import annotations

import json

import pytest

from voco.core.review_export import export_workspace
from voco.core.workspace import WorkspaceStore

LOCAL = {
    "host": "box",
    "cwd": "/r/proj",
    "repo": "proj",
    "branch": "main",
    "worktree": "/r/proj",
}


def store_with_diff():
    events = []
    store = WorkspaceStore(emit=lambda t, p: events.append((t, p)), now=lambda: 7.0)
    ws = store.resolve(LOCAL)
    page = store.upsert_diff(
        ws,
        ref="branch:main",
        title="diff",
        files=[{"path": "src/foo.py"}],
        source={"branch": "main"},
    )
    return store, ws, page, events


def test_add_finding_stamps_rev_and_emits():
    store, ws, page, events = store_with_diff()
    events.clear()
    f = store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "src/foo.py", "side": "new", "startLine": 3, "endLine": 3},
        text="unbounded loop",
        kind="concern",
        blocking=True,
    )
    assert f.rev == page.rev and f.status == "open" and f.blocking
    assert events[0][0] == "finding.added"
    assert events[0][1]["text"] == "unbounded loop"


def test_agent_status_restricted_human_can_withdraw():
    store, ws, page, _ = store_with_diff()
    f = store.add_finding(ws.key, page_id=page.page_id, anchor={"file": "x"}, text="t")
    # agent may set addressed
    store.set_finding_status(
        ws.key, f.finding_id, "addressed", commit="abc", agent=True
    )
    assert f.status == "addressed" and f.commit == "abc"
    # agent may NOT withdraw
    with pytest.raises(ValueError):
        store.set_finding_status(ws.key, f.finding_id, "withdrawn", agent=True)
    # human withdraw works
    store.withdraw_finding(ws.key, f.finding_id)
    assert f.status == "withdrawn"
    # and an agent cannot resurrect a withdrawn finding (review WARNING 5)
    with pytest.raises(ValueError):
        store.set_finding_status(ws.key, f.finding_id, "addressed", agent=True)


def test_finding_goes_stale_when_page_rev_bumps():
    store, ws, page, _ = store_with_diff()
    f = store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "src/foo.py", "startLine": 3},
        text="t",
    )
    assert f.rev == 1
    store.upsert_diff(
        ws,
        ref="branch:main",
        title="diff",
        files=[{"path": "src/foo.py"}, {"path": "src/bar.py"}],
        source={"branch": "main"},
    )
    assert page.rev == 2 and f.rev == 1  # finding kept, now stale


def test_identical_diff_republish_is_idempotent():
    store, ws, page, events = store_with_diff()
    events.clear()
    store.upsert_diff(
        ws,
        ref="branch:main",
        title="diff",
        files=[{"path": "src/foo.py"}],
        source={"branch": "main"},
    )
    assert page.rev == 1
    assert events == []


def test_findings_for_open_only_and_unknown_workspace():
    store, ws, page, _ = store_with_diff()
    a = store.add_finding(ws.key, page_id=page.page_id, anchor={"file": "x"}, text="a")
    store.add_finding(ws.key, page_id=page.page_id, anchor={"file": "y"}, text="b")
    store.set_finding_status(ws.key, a.finding_id, "addressed", agent=True)
    assert len(store.findings_for(ws.key)) == 2
    assert len(store.findings_for(ws.key, open_only=True)) == 1
    assert store.findings_for("nope") == []


def test_export_legacy_and_sidecar(tmp_path):
    store, ws, page, _ = store_with_diff()
    store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "src/foo.py", "side": "new", "startLine": 3, "endLine": 4},
        text="concern one",
    )
    withdrawn = store.add_finding(
        ws.key,
        page_id=page.page_id,
        anchor={"file": "src/foo.py", "side": "new", "startLine": 9, "endLine": 9},
        text="gone",
    )
    store.withdraw_finding(ws.key, withdrawn.finding_id)

    res = export_workspace(store, ws.key, data_dir=tmp_path, stamp="T")
    legacy = json.loads(
        (tmp_path / "workspaces" / "box%3A%2Fr%2Fproj" / "review-T.json").read_text()
    )
    assert legacy == [
        {
            "file": "src/foo.py",
            "side": "new",
            "startLine": 3,
            "endLine": 4,
            "concern": "concern one",
        }
    ]  # withdrawn excluded
    anchors = json.loads(
        (
            tmp_path / "workspaces" / "box%3A%2Fr%2Fproj" / "review-T.anchors.json"
        ).read_text()
    )
    assert len(anchors) == 2  # sidecar keeps withdrawn
    assert res["count"] == 1


# ---- page-less FILE findings (mk3.1 A3: the files view is not a page) ----


def test_add_file_finding_pageless():
    store, ws, _page, events = store_with_diff()
    events.clear()
    f = store.add_finding(
        ws.key,
        page_id=None,
        anchor={
            "kind": "file",
            "file": "src/foo.py",
            "startLine": 3,
            "endLine": 4,
            "exact": "def foo",
        },
        text="rename this",
        kind="nit",
    )
    assert f.page_id is None
    assert f.rev == 0  # no page rev to go stale against
    assert f.to_dict()["page_id"] is None
    assert events and events[0][0] == "finding.added"
    # page-less findings still ride pending_review (they are open work)
    ids = [i["id"] for i in ws.pending_review()]
    assert f.finding_id in ids


def test_pageless_finding_requires_file_anchor():
    store, ws, _page, _events = store_with_diff()
    with pytest.raises(ValueError, match="file anchor"):
        store.add_finding(ws.key, page_id=None, anchor={"exact": "x"}, text="t")
    with pytest.raises(ValueError, match="file anchor"):
        store.add_finding(
            ws.key,
            page_id=None,
            anchor={"kind": "file"},  # kind right, path missing
            text="t",
        )


def test_unknown_page_still_rejected():
    store, ws, _page, _events = store_with_diff()
    with pytest.raises(ValueError, match="unknown page"):
        store.add_finding(ws.key, page_id="pg-nope", anchor={"file": "x"}, text="t")
