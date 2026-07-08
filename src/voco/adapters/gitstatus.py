"""Local git facts for the rail (B1c) — status only, git only.

Concept from the reference's gitstatus.mjs, split along voco's seams:
the gh-side facts (PR state, checks, issues) already ride workspace
LINKS (U2a, optional-gh); this module is the LOCAL half — dirty /
staged / unstaged / untracked counts and ahead-behind vs upstream,
parsed from `git status --porcelain=v2 --branch`. Same degradation
stance as ghlink: no repo, no upstream, any failure → None or partial,
never an error. Injected Runner like every subprocess edge.
"""

from __future__ import annotations

from typing import Any

from voco.adapters.diffsource import Runner, _default_runner


def git_status(root: str, run: Runner = _default_runner) -> dict[str, Any] | None:
    """{dirty, staged, unstaged, untracked, ahead, behind} or None."""
    if not root:
        return None
    try:
        r = run(["git", "status", "--porcelain=v2", "--branch"], root)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    staged = unstaged = untracked = 0
    ahead: int | None = None
    behind: int | None = None
    for line in r.stdout.splitlines():
        if line.startswith("# branch.ab "):
            # "# branch.ab +A -B" — absent entirely when there is no upstream
            parts = line.split()
            try:
                ahead, behind = int(parts[2]), abs(int(parts[3]))
            except (IndexError, ValueError):
                pass
        elif line.startswith(("1 ", "2 ")):
            # changed/renamed entries: XY column — staged side, worktree side
            xy = line.split(" ", 2)[1]
            if len(xy) == 2:
                if xy[0] != ".":
                    staged += 1
                if xy[1] != ".":
                    unstaged += 1
        elif line.startswith("u "):
            unstaged += 1  # unmerged counts as work in the tree
        elif line.startswith("? "):
            untracked += 1
    return {
        "dirty": bool(staged or unstaged or untracked),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "ahead": ahead,
        "behind": behind,
    }
