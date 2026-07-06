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
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=cwd)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class DiffResolveError(Exception):
    """Soft, message-carrying failure — the route maps it to a 4xx hint."""


class DiffResolver:
    def __init__(self, runner: Runner = _default_runner) -> None:
        self._run = runner

    def _git(self, args: list[str], root: str) -> str:
        r = self._run(["git", *args], root)
        if r.returncode != 0:
            raise DiffResolveError(r.stderr.strip() or "git failed")
        return r.stdout

    def default_branch(self, root: str) -> str:
        """origin/HEAD's target, else main, else master — best effort."""
        try:
            ref = self._git(["symbolic-ref", "refs/remotes/origin/HEAD"], root).strip()
            if ref:
                return ref.rsplit("/", 1)[-1]
        except DiffResolveError:
            pass
        for cand in ("main", "master"):
            r = self._run(["git", "rev-parse", "--verify", cand], root)
            if r.returncode == 0:
                return cand
        return "HEAD"

    def resolve(self, source: dict, root: str) -> str:
        """Return unified-diff text for a source spec, cwd = workspace root."""
        if "pr" in source:
            n = str(source["pr"])
            r = self._run(["gh", "pr", "diff", n], root)
            if r.returncode != 0:
                raise DiffResolveError(
                    "gh pr diff failed (needs gh + network + auth): "
                    + (r.stderr.strip() or "unknown error")
                )
            return r.stdout
        if "staged" in source:
            return self._git(["diff", "--cached"], root)
        if "branch" in source:
            base = source.get("branch") or self.default_branch(root)
            merge_base = self._git(["merge-base", "HEAD", base], root).strip()
            if not merge_base:
                raise DiffResolveError(f"no merge-base with {base!r}")
            return self._git(["diff", f"{merge_base}..HEAD"], root)
        if "diff_file" in source:
            # Confinement is the route's job (same as docs); we only read.
            with open(source["diff_file"], encoding="utf-8", errors="replace") as fh:
                return fh.read()
        raise DiffResolveError("source must be one of pr|branch|staged|diff_file")


def source_ref(source: dict) -> str:
    """Stable identity for a diff source — same ref re-resolves in place."""
    for k in ("pr", "branch", "staged", "diff_file"):
        if k in source:
            return f"{k}:{source[k]}"
    return "diff:unknown"
