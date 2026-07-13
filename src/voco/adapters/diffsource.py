"""Diff resolution (SPEC-WORKBENCH §3.2, W1) — the impure edge that runs
git/gh in a workspace root and returns unified-diff text.

ROLE: turn a source spec ({pr}|{branch}|{staged}|{diff_file}) into diff
text, always with the workspace root as cwd (the invariant that makes
worktrees correct). argv only, never a shell. Injected runner keeps the
core resolver testable without a real repo.

INVARIANTS: `pr` needs `gh` + network + auth — a capability cell, not a
guarantee; failure returns a soft error string, never raises into a
request. `diff_file` is confined to the workspace root by the caller.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], str], RunResult]


def _default_runner(argv: list[str], cwd: str) -> RunResult:
    if not os.path.isdir(cwd):
        # A dead workspace root must say so — subprocess raises the same
        # FileNotFoundError as a missing binary, which would mislabel this
        # as "git: not installed" (dogfood 2026-07-06: stale roots).
        return RunResult(127, "", f"workspace root does not exist: {cwd}")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
    except FileNotFoundError:
        return RunResult(127, "", f"{argv[0]}: not installed (or not on PATH)")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


MAX_UNTRACKED = 200  # /dev/null diffs per worktree resolve (bounded work)


class DiffResolveError(Exception):
    """Soft, message-carrying failure — the route maps it to a 4xx hint."""


def valid_ref(ref: str) -> bool:
    """A conservative git ref shape: no leading dash (option injection), no
    whitespace or the chars git rev-names forbid. Not a full check-ref-format
    reimplementation — a shape gate before argv (review WARNING 7). Shared
    with the worktree adapter (branch/base names)."""
    return (
        bool(ref) and not ref.startswith("-") and not re.search(r"[\s~^:?*\[\\]", ref)
    )


class DiffResolver:
    def __init__(self, runner: Runner = _default_runner) -> None:
        self._run = runner

    def _git(self, args: list[str], root: str) -> str:
        r = self._run(["git", *args], root)
        if r.returncode != 0:
            raise DiffResolveError(r.stderr.strip() or "git failed")
        return r.stdout

    def default_branch(self, root: str) -> str:
        """The remote-tracking default (e.g. `origin/main`), else a local
        main/master, else HEAD. Keeps the FULL remote ref so the merge-base
        is against origin, not a stale local branch (review WARNING 6)."""
        try:
            ref = self._git(["symbolic-ref", "refs/remotes/origin/HEAD"], root).strip()
            if ref.startswith("refs/remotes/"):
                return ref[len("refs/remotes/") :]  # e.g. "origin/main"
        except DiffResolveError:
            pass
        for cand in ("origin/main", "origin/master", "main", "master"):
            r = self._run(["git", "rev-parse", "--verify", "--quiet", cand], root)
            if r.returncode == 0:
                return cand
        return "HEAD"

    def resolve(self, source: dict, root: str) -> str:
        """Return unified-diff text for a source spec, cwd = workspace root.
        Values that could be read as options are validated (review WARNING 7):
        argv already blocks shell injection; `--` terminators and shape
        checks block option injection."""
        if "pr" in source:
            n = str(source["pr"])
            if not n.isdigit():
                raise DiffResolveError("pr must be a number")
            r = self._run(["gh", "pr", "diff", n], root)
            if r.returncode != 0:
                raise DiffResolveError(
                    "gh pr diff failed (needs gh + network + auth): "
                    + (r.stderr.strip() or "unknown error")
                )
            return r.stdout
        if source.get("staged"):
            return self._git(["diff", "--cached"], root)
        if source.get("worktree"):
            # B2-16: the working tree vs HEAD — staged AND unstaged, the
            # diff of an agent mid-task. Branch mode (merge-base..HEAD)
            # shows only committed work. Untracked files ARE the agent's
            # work too (xai B1): each is diffed against /dev/null so new
            # files appear instead of silently vanishing.
            out = [self._git(["diff", "HEAD", "--"], root)]
            listed = self._git(
                ["ls-files", "--others", "--exclude-standard"], root
            ).splitlines()
            for path in listed[:MAX_UNTRACKED]:
                if not path or path.startswith("-"):
                    continue  # shape gate: a filename must never read as a flag
                # --no-index exits 1 when files differ — that IS success here.
                r = self._run(
                    ["git", "diff", "--no-index", "--", os.devnull, path], root
                )
                if r.returncode in (0, 1) and r.stdout:
                    out.append(r.stdout)
            if len(listed) > MAX_UNTRACKED:
                out.append(
                    f"\n# … {len(listed) - MAX_UNTRACKED} more untracked "
                    "file(s) not shown\n"
                )
            return "".join(out)
        if "branch" in source:
            base = source.get("branch") or self.default_branch(root)
            if not valid_ref(base):
                raise DiffResolveError(f"invalid base ref: {base!r}")
            # Triple-dot is Git's explicit merge-base diff: changes introduced
            # by HEAD since it forked from BASE. A two-dot BASE..HEAD diff would
            # include changes unique to both tips and can explode on long-lived
            # staging branches.
            return self._git(["diff", f"{base}...HEAD", "--"], root)
        if "diff_file" in source:
            # Confinement is the route's job (same as docs); we only read.
            with open(source["diff_file"], encoding="utf-8", errors="replace") as fh:
                return fh.read()
        raise DiffResolveError(
            "source must be one of pr|branch|staged|worktree|diff_file"
        )


def source_ref(source: dict) -> str:
    """Stable identity for a diff source — same ref re-resolves in place.
    MIRRORS resolve()'s precedence AND truthiness (xai W6: `{worktree:
    false}` must not mint a ref that resolve() would refuse)."""
    if "pr" in source:
        return f"pr:{source['pr']}"
    if source.get("staged"):
        return "staged:True"
    if source.get("worktree"):
        return "worktree:True"
    if "branch" in source:
        return f"branch:{source['branch']}"
    if "diff_file" in source:
        return f"diff_file:{source['diff_file']}"
    return "diff:unknown"
