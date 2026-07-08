"""U2a (DESIGN-DECK rev 5) — GitHub issue/PR links on workspaces.

Tests at the command seam per the working rules. The gh edge is
OPTIONAL by decision (grill 2026-07-07): every adapter failure returns
None — link detection may never surface an error.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from voco.adapters.diffsource import RunResult
from voco.adapters.ghlink import detect
from voco.core.workspace import WorkspaceStore
from voco.daemon import Daemon

# ---- adapter: ghlink.detect ------------------------------------------------


PR_JSON = json.dumps(
    [
        {
            "number": 7,
            "url": "https://github.com/o/r/pull/7",
            "title": "fix auth",
            "closingIssuesReferences": [
                {"number": 3, "url": "https://github.com/o/r/issues/3", "title": "bug"}
            ],
        }
    ]
)


def runner_returning(stdout: str, code: int = 0):
    calls: list[list[str]] = []

    def run(argv: list[str], cwd: str) -> RunResult:
        calls.append(argv)
        return RunResult(code, stdout, "" if code == 0 else "boom")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_detect_finds_pr_and_closing_issue():
    run = runner_returning(PR_JSON)
    links = detect("/repo", "fix-auth", run=run)
    assert links == {
        "pr": {
            "number": 7,
            "url": "https://github.com/o/r/pull/7",
            "title": "fix auth",
        },
        "issue": {
            "number": 3,
            "url": "https://github.com/o/r/issues/3",
            "title": "bug",
        },
    }
    assert run.calls[0][:3] == ["gh", "pr", "list"]
    assert "--head" in run.calls[0]


def test_detect_pr_without_issue():
    run = runner_returning(json.dumps([{"number": 9, "url": "u", "title": "t"}]))
    links = detect("/repo", "b", run=run)
    assert links == {"pr": {"number": 9, "url": "u", "title": "t"}}


def test_detect_falls_back_when_rich_fields_unsupported():
    calls: list[list[str]] = []

    def run(argv: list[str], cwd: str) -> RunResult:
        calls.append(argv)
        if "closingIssuesReferences" in " ".join(argv):
            return RunResult(1, "", "unknown JSON field")
        return RunResult(0, json.dumps([{"number": 2, "url": "u", "title": "t"}]), "")

    links = detect("/repo", "b", run=run)
    assert links == {"pr": {"number": 2, "url": "u", "title": "t"}}
    assert len(calls) == 2


def test_detect_is_silent_on_every_failure():
    # no gh / non-zero on both attempts
    assert detect("/repo", "b", run=runner_returning("", code=127)) is None
    # bad json
    assert detect("/repo", "b", run=runner_returning("not json")) is None
    # no open PR
    assert detect("/repo", "b", run=runner_returning("[]")) is None
    # branch shapes that must never reach argv
    hostile = runner_returning(PR_JSON)
    assert detect("/repo", "--upload-pack=x", run=hostile) is None
    assert detect("/repo", "", run=hostile) is None
    assert hostile.calls == []  # type: ignore[attr-defined]


def test_detect_survives_runner_oserror():
    def run(argv: list[str], cwd: str) -> RunResult:
        raise OSError("fs gone")

    assert detect("/repo", "b", run=run) is None


# ---- store: links field ----------------------------------------------------


def make_store():
    events: list[tuple[str, dict]] = []
    store = WorkspaceStore(emit=lambda t, p: events.append((t, p)))
    ws = store.resolve(
        {"host": "h", "cwd": "/w", "worktree": "/w", "repo": "w", "branch": "main"}
    )
    return store, ws, events


def test_set_links_sets_clears_and_converges():
    store, ws, events = make_store()
    events.clear()
    store.set_links(ws.key, {"pr": {"number": 7, "url": "u", "title": "t"}})
    assert ws.links["pr"]["number"] == 7
    assert events and events[-1][0] == "workspace.updated"
    assert events[-1][1]["links"]["pr"]["number"] == 7
    # exact duplicate is a true no-op (at-least-once house style)
    events.clear()
    store.set_links(ws.key, {"pr": {"number": 7, "url": "u", "title": "t"}})
    assert events == []
    # None clears one kind, leaves the other
    store.set_links(ws.key, {"issue": {"number": 3}})
    store.set_links(ws.key, {"pr": None})
    assert "pr" not in ws.links and ws.links["issue"]["number"] == 3


def test_set_links_validates():
    store, ws, _ = make_store()
    with pytest.raises(ValueError):
        store.set_links(ws.key, {"pr": {"url": "no-number"}})
    with pytest.raises(ValueError):
        store.set_links(ws.key, {"badkind": {"number": 1}})
    with pytest.raises(ValueError):
        store.set_links("nope:/x", {"pr": {"number": 1}})


def test_links_ride_meta_dump_and_restore():
    store, ws, _ = make_store()
    store.set_links(ws.key, {"pr": {"number": 7, "url": "u"}})
    assert ws.meta()["links"]["pr"]["number"] == 7
    dumped = store.dump_workspace(ws)
    fresh = WorkspaceStore()
    restored = fresh.restore_workspace(dumped)
    assert restored is not None and restored.links["pr"]["number"] == 7
    # pre-links manifests (no "links" key) restore clean
    del dumped["links"]
    older = WorkspaceStore().restore_workspace(dumped)
    assert older is not None and older.links == {}


# ---- daemon: workspace.link + attach.snippet --------------------------------


@pytest.fixture
def daemon() -> Daemon:
    return Daemon({}, no_audio=True)


def make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return repo


async def opened_key(daemon, tmp_path) -> str:
    repo = make_repo(tmp_path)
    out = await daemon._control("workspace.open", {"path": str(repo)})
    return out["workspace"]


async def test_workspace_link_manual_set_and_clear(daemon, tmp_path):
    key = await opened_key(daemon, tmp_path)
    out = await daemon._control(
        "workspace.link",
        {"workspace": key, "issue": {"number": 3, "url": "u"}},
    )
    assert out["links"]["issue"]["number"] == 3
    out = await daemon._control("workspace.link", {"workspace": key, "issue": None})
    assert out["links"] == {}


async def test_workspace_link_unknown_workspace(daemon):
    with pytest.raises(ValueError, match="unknown workspace"):
        await daemon._control("workspace.link", {"workspace": "h:/ghost"})


async def test_workspace_link_detect_applies_and_caches(daemon, tmp_path):
    key = await opened_key(daemon, tmp_path)
    calls = []

    def fake_detect(root: str, branch: str):
        calls.append((root, branch))
        return {"pr": {"number": 7, "url": "u", "title": "t"}}

    daemon._ghlink_detect = fake_detect
    out = await daemon._control("workspace.link", {"workspace": key, "detect": True})
    assert out["links"]["pr"]["number"] == 7
    assert len(calls) == 1
    # cached: a second detect does not re-run gh
    await daemon._control("workspace.link", {"workspace": key, "detect": True})
    assert len(calls) == 1
    # force bypasses the cache
    await daemon._control(
        "workspace.link", {"workspace": key, "detect": True, "force": True}
    )
    assert len(calls) == 2


async def test_workspace_link_detect_never_overwrites_manual(daemon, tmp_path):
    key = await opened_key(daemon, tmp_path)
    await daemon._control(
        "workspace.link", {"workspace": key, "pr": {"number": 1, "url": "manual"}}
    )
    daemon._ghlink_detect = lambda root, branch: {
        "pr": {"number": 7, "url": "detected"},
        "issue": {"number": 3},
    }
    out = await daemon._control(
        "workspace.link", {"workspace": key, "detect": True, "force": True}
    )
    assert out["links"]["pr"]["url"] == "manual"  # manual wins
    assert out["links"]["issue"]["number"] == 3  # detect fills the gap


async def test_workspace_link_detect_silent_when_nothing_found(daemon, tmp_path):
    key = await opened_key(daemon, tmp_path)
    daemon._ghlink_detect = lambda root, branch: None
    out = await daemon._control("workspace.link", {"workspace": key, "detect": True})
    assert out["links"] == {}  # no error surfaced anywhere


async def test_workspace_link_detect_skips_sessionspaces(daemon):
    ws = daemon.workspaces.resolve({"host": "h", "cwd": "/tmp/nowhere"})
    daemon._ghlink_detect = lambda root, branch: pytest.fail("must not run gh")
    out = await daemon._control("workspace.link", {"workspace": ws.key, "detect": True})
    assert out["links"] == {}


async def test_attach_snippet_names_the_daemon(daemon):
    out = await daemon._control("attach.snippet", {})
    assert out["url"].startswith("http://127.0.0.1:")
    assert out["mcp"]["mcpServers"]["voco"]["command"] == "voco-mcp"
    assert out["mcp"]["mcpServers"]["voco"]["env"]["VOCO_URL"] == out["url"]
    assert "RemoteForward" in out["remote"]
