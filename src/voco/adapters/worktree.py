"""Worktrees first-class (SPEC-WORKBENCH §11 W3, decision 8) — create a
sibling worktree per agent, remove it on kill ONLY when clean.

ROLE: the git-worktree edge for `voco new --worktree` and the rail's
spawn affordance. argv only, `git -C <repo>` (no cwd juggling), injected
runner — same Runner shape as the tmux adapter so daemon tests fake both
with one recorder.

INVARIANTS: refs pass the same shape gate as diff resolution (no option
injection); worktrees land as SIBLINGS of the main checkout
(`<parent>/<repo>-<branch-slug>`), never inside it; removal REFUSES a
dirty tree (uncommitted work is the agent's work product — losing it is
the one unforgivable failure) and only ever removes paths this daemon
run created.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from voco.adapters.diffsource import valid_ref


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], RunResult]


def _default_runner(argv: list[str]) -> RunResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return RunResult(127, "", f"{argv[0]}: not installed (or not on PATH)")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class WorktreeError(Exception):
    """Soft, message-carrying failure — control surfaces it to the caller."""


def branch_slug(branch: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-").lower()
    return slug or "work"


class WorktreeManager:
    def __init__(self, runner: Runner = _default_runner) -> None:
        self._run = runner

    def _git(self, repo: str, args: list[str]) -> str:
        r = self._run(["git", "-C", repo, *args])
        if r.returncode != 0:
            raise WorktreeError(r.stderr.strip() or f"git {args[0]} failed")
        return r.stdout

    def _toplevel(self, repo: str) -> Path:
        top = self._git(repo, ["rev-parse", "--show-toplevel"]).strip()
        if not top:
            raise WorktreeError(f"not a git checkout: {repo}")
        return Path(top)

    def _branch_exists(self, repo: str, branch: str) -> bool:
        r = self._run(
            [
                "git",
                "-C",
                repo,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ]
        )
        return r.returncode == 0

    def add(self, repo: str, branch: str, base: str | None = None) -> str:
        """Create `<parent>/<repo>-<branch-slug>` on `branch` and return its
        path. An existing branch is checked out; a new one forks from
        `base` (default: the current HEAD). The path must not exist yet —
        colliding with anything on disk is an error, never a reuse."""
        if not valid_ref(branch):
            raise WorktreeError(f"invalid branch name: {branch!r}")
        if base is not None and not valid_ref(base):
            raise WorktreeError(f"invalid base ref: {base!r}")
        top = self._toplevel(repo)
        path = top.parent / f"{top.name}-{branch_slug(branch)}"
        if path.exists():
            raise WorktreeError(f"{path} already exists")
        if self._branch_exists(repo, branch):
            if base is not None:
                raise WorktreeError(
                    f"branch {branch!r} already exists; --from only applies "
                    "to a new branch"
                )
            self._git(repo, ["worktree", "add", str(path), branch])
        else:
            args = ["worktree", "add", "-b", branch, str(path)]
            if base is not None:
                args.append(base)
            self._git(repo, args)
        return str(path)

    def is_clean(self, path: str) -> bool:
        return not self._git(path, ["status", "--porcelain"]).strip()

    def remove(self, path: str) -> None:
        """Remove a CLEAN worktree; a dirty one raises and stays. No
        --force, ever — git's own dirty check is the second belt."""
        if not self.is_clean(path):
            raise WorktreeError(f"worktree {path} has uncommitted work; kept")
        self._git(path, ["worktree", "remove", str(path)])
