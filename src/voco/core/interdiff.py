"""Inter-diff (SPEC-WORKBENCH §11 W5) — re-review support.

Ported from diff-annotate's `lib/server/interdiff.mjs` (the oracle).
When a diff page is re-pushed, compare the previous rev's parsed files
with the new ones PER FILE and summarize what moved between revs — a
reviewer returning to an iterated branch re-checks only what changed
since the rev they reviewed, and each older finding can say whether its
area was touched. Content comparison is per-file hunk identity (a hash
of the file's hunks), never line heuristics:

  changed   — in both revs, hunk content differs
  added     — enters the diff at the new rev
  removed   — no longer part of the diff (merged away / rebased out)
  unchanged — in both revs, identical hunks

"removed" counts as a touched area on purpose: a finding whose diff
lines vanished needs a re-look at least as much as one whose lines
moved.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _file_hash(file: dict[str, Any]) -> str:
    payload = json.dumps(file.get("hunks") or [], separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()


def compute_interdiff(
    prev_files: list[dict], next_files: list[dict], since_rev: int
) -> dict[str, Any]:
    """Summarize what moved between two parsed diffs (core.diff output);
    `since_rev` labels the old rev."""
    prev = {f["path"]: _file_hash(f) for f in prev_files or []}
    nxt = {f["path"]: _file_hash(f) for f in next_files or []}
    changed: list[str] = []
    added: list[str] = []
    unchanged: list[str] = []
    for path, digest in nxt.items():
        if path not in prev:
            added.append(path)
        elif prev[path] != digest:
            changed.append(path)
        else:
            unchanged.append(path)
    removed = [p for p in prev if p not in nxt]
    return {
        "since_rev": since_rev,
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
    }


def area_touched(interdiff: dict[str, Any] | None, file: str | None) -> bool:
    """Was this file's area touched between the revs this interdiff spans?"""
    if not interdiff or file is None:
        return False
    f = str(file)
    return (
        f in interdiff.get("changed", ())
        or f in interdiff.get("added", ())
        or f in interdiff.get("removed", ())
    )
