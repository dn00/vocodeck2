"""W3 — worktrees first-class (SPEC-WORKBENCH §11, decision 8).

Adapter: sibling-path creation, new-vs-existing branch, ref shape gates,
clean-only removal. Daemon: spawn-in-worktree lifecycle (create → spawn →
record; failed spawn cleans up; kill reaps clean, keeps dirty)."""

from __future__ import annotations

import pytest

from voco.adapters.worktree import (
    RunResult,
    WorktreeError,
    WorktreeManager,
    branch_slug,
)
from voco.daemon import Daemon


class GitFake:
    """Scripted git: records argv, answers by subcommand."""

    def __init__(self, tmp_path, *, branch_exists=False, dirty=False) -> None:
        self.calls: list[list[str]] = []
        self.top = tmp_path / "repo"
        self.branch_exists = branch_exists
        self.dirty = dirty
        self.fail_worktree_add = False

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        sub = argv[3] if argv[:2] == ["git", "-C"] else argv[1]
        if sub == "rev-parse" and "--show-toplevel" in argv:
            return RunResult(0, f"{self.top}\n", "")
        if sub == "rev-parse":  # branch existence probe
            return RunResult(0 if self.branch_exists else 1, "", "")
        if sub == "status":
            return RunResult(0, " M dirty.py\n" if self.dirty else "", "")
        if sub == "worktree" and self.fail_worktree_add:
            return RunResult(128, "", "fatal: scripted failure")
        return RunResult(0, "", "")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def test_add_new_branch_forks_from_base(tmp_path, repo):
    fake = GitFake(tmp_path)
    path = WorktreeManager(fake).add(str(repo), "feat-x", base="main")
    assert path == str(tmp_path / "repo-feat-x")
    assert [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "-b",
        "feat-x",
        path,
        "main",
    ] in fake.calls


def test_add_existing_branch_checks_it_out(tmp_path, repo):
    fake = GitFake(tmp_path, branch_exists=True)
    path = WorktreeManager(fake).add(str(repo), "feat-x")
    assert ["git", "-C", str(repo), "worktree", "add", path, "feat-x"] in fake.calls


def test_add_existing_branch_rejects_from(tmp_path, repo):
    fake = GitFake(tmp_path, branch_exists=True)
    with pytest.raises(WorktreeError, match="already exists; --from"):
        WorktreeManager(fake).add(str(repo), "feat-x", base="main")


def test_add_rejects_bad_refs_and_existing_path(tmp_path, repo):
    fake = GitFake(tmp_path)
    mgr = WorktreeManager(fake)
    with pytest.raises(WorktreeError, match="invalid branch"):
        mgr.add(str(repo), "--upload-pack=evil")
    with pytest.raises(WorktreeError, match="invalid base"):
        mgr.add(str(repo), "ok", base="-bad")
    (tmp_path / "repo-taken").mkdir()
    with pytest.raises(WorktreeError, match="already exists"):
        mgr.add(str(repo), "taken")


def test_remove_refuses_dirty_tree(tmp_path):
    fake = GitFake(tmp_path, dirty=True)
    with pytest.raises(WorktreeError, match="uncommitted work"):
        WorktreeManager(fake).remove(str(tmp_path / "repo-feat-x"))
    assert not any("remove" in c for c in fake.calls)  # never reached git


def test_remove_clean_tree(tmp_path):
    fake = GitFake(tmp_path)
    wt = str(tmp_path / "repo-feat-x")
    WorktreeManager(fake).remove(wt)
    assert ["git", "-C", wt, "worktree", "remove", wt] in fake.calls


def test_branch_slug():
    assert branch_slug("feat/One Two") == "feat-one-two"
    assert branch_slug("///") == "work"


# ---- daemon lifecycle -----------------------------------------------------------


class TmuxFake:
    def __init__(self) -> None:
        self.spawns: list[dict] = []
        self.killed: list[str] = []
        self.fail_spawn = False

    def spawn(self, harness, name, cwd=None, host=None) -> str:
        if self.fail_spawn:
            raise RuntimeError("harness died at spawn")
        self.spawns.append({"harness": harness, "name": name, "cwd": cwd})
        return f"voco-{name}"

    def kill(self, name, host=None) -> None:
        self.killed.append(name)


class WtFake:
    def __init__(self, tmp_path, *, dirty=False) -> None:
        self.tmp = tmp_path
        self.dirty = dirty
        self.added: list[tuple] = []
        self.removed: list[str] = []

    def add(self, repo, branch, base=None) -> str:
        self.added.append((repo, branch, base))
        return str(self.tmp / f"repo-{branch}")

    def remove(self, path) -> None:
        if self.dirty:
            raise WorktreeError(f"worktree {path} has uncommitted work; kept")
        self.removed.append(path)


@pytest.fixture
def daemon(tmp_path):
    d = Daemon({}, no_audio=True)
    d._tmux_mgr = TmuxFake()
    d._worktree_mgr = WtFake(tmp_path)
    return d


async def test_spawn_with_worktree_creates_then_spawns_inside(daemon, tmp_path):
    result = await daemon._control(
        "session.spawn",
        {"harness": "claude", "cwd": "/repo/a", "worktree": {"branch": "feat-x"}},
    )
    assert daemon._worktree_mgr.added == [("/repo/a", "feat-x", None)]
    assert daemon._tmux_mgr.spawns[0]["cwd"] == str(tmp_path / "repo-feat-x")
    assert daemon._tmux_mgr.spawns[0]["name"] == "feat-x"  # branch names it
    assert result == {
        "tmux_session": "voco-feat-x",
        "worktree": str(tmp_path / "repo-feat-x"),
    }


async def test_spawn_worktree_requires_cwd_and_local(daemon):
    with pytest.raises(ValueError, match="source repo cwd"):
        await daemon._control(
            "session.spawn", {"harness": "claude", "worktree": {"branch": "x"}}
        )
    with pytest.raises(ValueError, match="local-only"):
        await daemon._control(
            "session.spawn",
            {
                "harness": "claude",
                "cwd": "/r",
                "host": "ws",
                "worktree": {"branch": "x"},
            },
        )


async def test_failed_spawn_removes_the_fresh_worktree(daemon):
    daemon._tmux_mgr.fail_spawn = True
    with pytest.raises(RuntimeError, match="died at spawn"):
        await daemon._control(
            "session.spawn",
            {"harness": "claude", "cwd": "/repo/a", "worktree": {"branch": "f"}},
        )
    assert daemon._worktree_mgr.removed  # no stranded worktree
    assert daemon._spawned_worktrees == {}


async def test_kill_reaps_clean_worktree(daemon, tmp_path):
    await daemon._control(
        "session.spawn",
        {"harness": "claude", "cwd": "/repo/a", "worktree": {"branch": "feat-x"}},
    )
    result = await daemon._control("session.kill", {"name": "voco-feat-x"})
    assert result["worktree_removed"] is True
    assert daemon._spawned_worktrees == {}


async def test_kill_keeps_dirty_worktree(daemon, tmp_path):
    await daemon._control(
        "session.spawn",
        {"harness": "claude", "cwd": "/repo/a", "worktree": {"branch": "feat-x"}},
    )
    daemon._worktree_mgr.dirty = True
    result = await daemon._control("session.kill", {"name": "voco-feat-x"})
    assert "uncommitted work" in result["worktree_kept"]
    # Still tracked: a later (clean) kill may reap it.
    assert daemon._spawned_worktrees == {"voco-feat-x": str(tmp_path / "repo-feat-x")}


async def test_kill_without_worktree_reports_nothing(daemon):
    result = await daemon._control("session.kill", {"name": "voco-plain"})
    assert result == {}
