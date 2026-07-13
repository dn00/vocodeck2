"""GitHub link detection (DESIGN-DECK rev 5, U2a) — the OPTIONAL gh edge.

ROLE: find the open PR for a branch (`gh pr list --head <branch>`) and,
when one exists, the issue it closes. gh is optional BY DECISION (grill
2026-07-07): every failure — no gh binary, unauthenticated, offline,
old gh, bad JSON — returns None, and callers treat None as "no link",
never as an error to surface. git facts never come from here (identity
and diffsource own those); this adapter only decorates a workspace.

Runner is injected like diffsource/tmux so tests fake subprocess; the
default runner is diffsource's (same dead-root and missing-binary
honesty, argv only, never a shell).
"""

from __future__ import annotations

import json
from typing import Any

from voco.adapters.diffsource import Runner, _default_runner, valid_ref

_FIELDS_RICH = "number,url,title,closingIssuesReferences"
_FIELDS_PLAIN = "number,url,title"


def _link(raw: Any) -> dict[str, Any] | None:
    """Normalize one gh object to {number, url?, title?}; number required."""
    if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
        return None
    out: dict[str, Any] = {"number": raw["number"]}
    for k in ("url", "title"):
        if isinstance(raw.get(k), str) and raw[k]:
            out[k] = raw[k]
    return out


def detect(root: str, branch: str, run: Runner = _default_runner) -> dict | None:
    """{pr: {...}, issue?: {...}} for the branch's open PR, else None.
    The branch is shape-gated before it reaches argv (house rule)."""
    if not root or not valid_ref(branch):
        return None
    argv = ["gh", "pr", "list", "--head", branch, "--limit", "1", "--json"]
    try:
        r = run([*argv, _FIELDS_RICH], root)
        if r.returncode != 0:
            # Older gh without the field (or any other refusal): one plain
            # retry so a missing extra never costs the PR link itself.
            r = run([*argv, _FIELDS_PLAIN], root)
        if r.returncode != 0:
            return None
        prs = json.loads(r.stdout)
    except Exception:
        # Deliberately blind (xai BLOCKER 1): gh is optional BY DECISION —
        # a hung gh (TimeoutExpired), a raising runner, bad JSON, anything:
        # the answer is "no link", never an error.
        return None
    if not isinstance(prs, list) or not prs:
        return None
    pr = _link(prs[0])
    if pr is None:
        return None
    links: dict[str, Any] = {"pr": pr}
    closing = prs[0].get("closingIssuesReferences")
    if isinstance(closing, list) and closing:
        issue = _link(closing[0])
        if issue is not None:
            links["issue"] = issue
    return links
