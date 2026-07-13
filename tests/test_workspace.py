"""Workspace/pages core (SPEC-WORKBENCH §2–§3) + display state (§6)."""

from __future__ import annotations

import pytest

from voco.core.agent_state import display_state
from voco.core.limits import MAX_SCREEN_BYTES
from voco.core.workspace import WorkspaceStore

LOCAL = {
    "host": "box",
    "cwd": "/home/d/proj",
    "repo": "proj",
    "branch": "main",
    "worktree": "/home/d/proj",
    "common_dir": "/home/d/proj/.git",
}


def store_and_events():
    events: list[tuple[str, dict]] = []
    return WorkspaceStore(
        emit=lambda t, p: events.append((t, p)), now=lambda: 42.0
    ), events


def test_resolve_keys_by_root_not_branch():
    store, _ = store_and_events()
    ws = store.resolve(LOCAL)
    assert ws.kind == "workspace"
    assert ws.key == "box:/home/d/proj"
    assert ws.branch == "main"
    # git switch: same workspace, branch is display state only
    ws2 = store.resolve({**LOCAL, "branch": "feature"})
    assert ws2 is ws
    assert ws.branch == "feature"


def test_branch_change_emits_workspace_updated():
    store, events = store_and_events()
    store.resolve(LOCAL)
    events.clear()
    store.resolve({**LOCAL, "branch": "feature"})
    assert events == [
        (
            "workspace.updated",
            {
                "key": "box:/home/d/proj",
                "kind": "workspace",
                "name": "proj",
                "repo": "proj",
                "branch": "feature",
                "common_dir": "/home/d/proj/.git",
                "links": {},
                "git": None,
                "pages": 0,
            },
        )
    ]


def test_worktrees_are_distinct_workspaces_sharing_common_dir():
    store, _ = store_and_events()
    a = store.resolve(LOCAL)
    b = store.resolve(
        {
            **LOCAL,
            "cwd": "/home/d/proj-wt",
            "worktree": "/home/d/proj-wt",
            "branch": "feature",
        }
    )
    assert a is not b
    assert a.common_dir == b.common_dir  # rail groups them


def test_no_repo_lands_in_sessionspace():
    store, _ = store_and_events()
    ws = store.resolve({"host": "box", "cwd": "/tmp/scratch"})
    assert ws.kind == "sessionspace"
    assert ws.key == "box:/tmp/scratch"
    assert ws.repo is None


def test_remote_same_path_does_not_collide():
    store, _ = store_and_events()
    a = store.resolve(LOCAL)
    b = store.resolve({**LOCAL, "host": "workspace-vm"})
    assert a is not b


def test_screen_upsert_show_and_append_bump_rev():
    store, events = store_and_events()
    page = store.upsert_screen(
        LOCAL,
        session_id="s1",
        call_name="Helena",
        markdown="# plan",
        title="Plan",
        mode="show",
    )
    assert page.pinned and page.scope == "agent" and page.rev == 1
    assert page.ref == "screen:Helena"

    store.upsert_screen(
        LOCAL,
        session_id="s1",
        call_name="Helena",
        markdown="more",
        title=None,
        mode="append",
    )
    assert page.rev == 2
    assert page.data["markdown"] == "# plan\nmore"

    store.upsert_screen(
        LOCAL,
        session_id="s2",  # re-registered agent: id refreshes, ref is stable
        call_name="Helena",
        markdown="fresh",
        title="Fresh",
        mode="show",
    )
    assert page.rev == 3
    assert page.session_id == "s2"
    assert page.data["markdown"] == "fresh"

    actions = [p["action"] for t, p in events if t == "page.updated"]
    assert actions == ["added", "updated", "updated"]


def test_screen_limit_is_utf8_aware_and_rejection_has_no_side_effects():
    store, events = store_and_events()
    exact = "é" * (MAX_SCREEN_BYTES // 2)
    page = store.upsert_screen(
        LOCAL,
        session_id="s1",
        call_name="Helena",
        markdown=exact,
        title="Exact",
        mode="show",
    )
    assert page.data["markdown"] == exact
    before = (page.rev, dict(page.data), list(events))

    with pytest.raises(ValueError, match="screen exceeds maximum size"):
        store.upsert_screen(
            LOCAL,
            session_id="s1",
            call_name="Helena",
            markdown="x",
            title=None,
            mode="append",
        )

    assert (page.rev, page.data, events) == before


def test_restore_drops_oversized_screen_markdown():
    store, _ = store_and_events()
    ws = store.resolve(LOCAL)
    page = store.upsert_screen(
        LOCAL,
        session_id="s1",
        call_name="Helena",
        markdown="ok",
        title="Screen",
        mode="show",
    )
    dumped = store.dump_workspace(ws)
    dumped["pages"][0]["data"]["markdown"] = "é" * MAX_SCREEN_BYTES

    restored, _ = store_and_events()
    ws2 = restored.restore_workspace(dumped)
    assert ws2 is not None
    assert ws2.pages[page.page_id].data["markdown"] == ""


def test_terminal_reattach_bumps_rev_for_browser_cache():
    store, _ = store_and_events()
    page = store.upsert_terminal(
        LOCAL, session_id="s1", call_name="Helena", mode="mirror", handle="old"
    )
    assert page.rev == 1
    store.upsert_terminal(
        LOCAL, session_id="s2", call_name="Helena", mode="stream", handle="new"
    )
    assert page.rev == 2
    assert page.session_id == "s2" and page.data["handle"] == "new"


def test_identical_terminal_registration_is_a_noop():
    store, events = store_and_events()
    page = store.upsert_terminal(
        LOCAL, session_id="s1", call_name="Helena", mode="mirror", handle="term"
    )
    events.clear()
    store.upsert_terminal(
        LOCAL, session_id="s1", call_name="Helena", mode="mirror", handle="term"
    )
    assert page.rev == 1 and events == []


def test_doc_push_path_xor_content_and_rev_bump():
    store, _ = store_and_events()
    ws = store.resolve(LOCAL)
    with pytest.raises(ValueError):
        store.push_doc(ws)
    with pytest.raises(ValueError):
        store.push_doc(ws, path="/a.md", content="x")

    doc = store.push_doc(ws, path="/home/d/proj/DESIGN.md")
    assert doc.title == "DESIGN.md" and doc.rev == 1 and doc.scope == "workspace"
    again = store.push_doc(ws, path="/home/d/proj/DESIGN.md")
    assert again is doc and doc.rev == 2

    virt = store.push_doc(ws, name="notes", content="# hi")
    assert virt.data == {"content": "# hi"}


def test_close_reopen_and_pinned_guard():
    store, _ = store_and_events()
    ws = store.resolve(LOCAL)
    doc = store.push_doc(ws, name="notes", content="x")
    store.set_closed(doc.page_id, True)
    assert doc.closed
    # re-push resurfaces
    store.push_doc(ws, name="notes", content="y")
    assert not doc.closed

    screen = store.upsert_screen(
        LOCAL, session_id="s1", call_name="Iris", markdown="", title=None, mode="show"
    )
    with pytest.raises(ValueError):
        store.set_closed(screen.page_id, True)


def test_snapshot_carries_metadata_never_content():
    store, _ = store_and_events()
    ws = store.resolve(LOCAL)
    store.push_doc(ws, name="notes", content="SECRET-BODY")
    snap = store.snapshot()
    assert snap[0]["name"] == "proj"
    (page_meta,) = snap[0]["pages"]
    assert page_meta["type"] == "doc"
    assert "SECRET-BODY" not in str(snap)


# ---- display state (§6): total precedence -----------------------------------


def test_display_state_precedence_table():
    f = display_state
    assert f(bridge_state="working", pane_hint="shell") == "gone"
    assert f(bridge_state="parked", handle_alive=False) == "gone"
    assert f(bridge_state="working", pane_hint="waiting") == "blocked"
    # parked overrides waiting: a parked agent is not blocked
    assert f(bridge_state="parked", pane_hint="waiting") == "listening"
    assert f(bridge_state="working") == "working"
    assert f(bridge_state="idle", pane_hint="working") == "working"
    assert f(bridge_state="parked") == "listening"
    assert f(bridge_state="idle", idle_for_s=700) == "stale"
    assert f(bridge_state="idle") == "idle"
