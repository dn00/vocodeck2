"""Workspaces + pages (SPEC-WORKBENCH §2–§3).

ROLE: pure domain model for the workbench content plane — workspace
identity (checkout-keyed, sessionspace fallback), typed pages with revs,
and the screen-verb upsert. Transport-free and fs-free: git facts and
file contents arrive as arguments (derive-don't-ask; the server layer
resolves paths and reads disks).

INVARIANTS:
- Workspace key = (host, checkout root): root alone per the grill
  decision (branch is display state, never identity); host qualifies it
  because remote sessions carry paths the daemon cannot realpath and two
  hosts may share a path string (workbench decision log 20, extended).
- A session with no repo lands in a SESSIONSPACE keyed by (host, cwd) —
  agent-scoped pages only, no review surfaces.
- Page identity within a workspace is `(type, ref)`; re-push bumps rev
  (screen `show` bumps, `append` grows the same rev). Closing hides,
  never deletes.
- Rev history is never rewritten; findings staleness (W1) rides revs.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

PageScope = Literal["workspace", "agent"]

# Page types shipped in W0/W1; the set is data, additive by design.
PAGE_TYPES = {"screen", "terminal", "diff", "doc"}
PINNED_TYPES = {"screen", "terminal", "diff"}

FindingKind = Literal["concern", "question", "nit"]
FindingStatus = Literal["open", "addressed", "disputed", "wont-fix", "withdrawn"]
# Statuses an AGENT may set via the bridge (never "withdrawn" — that is the
# human's remove) and never "open" (agents don't re-open).
AGENT_STATUSES = {"addressed", "disputed", "wont-fix"}


@dataclass
class Finding:
    """A human annotation on a page (SPEC-WORKBENCH §2). Diff anchors are
    {file, side, startLine, endLine} — camelCase, byte-compatible with
    diff-annotate's output for downstream consumers (§3.3 export)."""

    finding_id: str
    page_id: str
    rev: int
    anchor: dict[str, Any]
    text: str
    kind: FindingKind = "concern"
    blocking: bool = False
    status: FindingStatus = "open"
    note: str | None = None
    commit: str | None = None
    answer: str | None = None
    created_ts: float = 0.0
    updated_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "page_id": self.page_id,
            "rev": self.rev,
            "anchor": self.anchor,
            "text": self.text,
            "kind": self.kind,
            "blocking": self.blocking,
            "status": self.status,
            "note": self.note,
            "commit": self.commit,
            "answer": self.answer,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
        }


@dataclass
class Ask:
    """A question from the in-page chat (SPEC-WORKBENCH §4.3), routed to the
    workspace's primary agent and answered in markdown."""

    ask_id: str
    text: str
    context: dict[str, Any] | None = None
    answer: str | None = None
    created_ts: float = 0.0
    answered_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ask_id": self.ask_id,
            "text": self.text,
            "context": self.context,
            "answer": self.answer,
            "created_ts": self.created_ts,
            "answered_ts": self.answered_ts,
        }


@dataclass
class Page:
    page_id: str
    type: str
    ref: str  # identity within the workspace: "screen:Helena", "doc:<path>"
    title: str
    scope: PageScope
    rev: int = 1
    pinned: bool = False
    closed: bool = False
    # Agent-scoped pages: the owning agent (call_name is the stable half;
    # session_id refreshes on re-register).
    session_id: str | None = None
    call_name: str | None = None
    # Type-specific payload: screen -> {markdown, screen_title};
    # doc -> {path} | {content}; diff (W1) -> {diff_text, source}.
    data: dict[str, Any] = field(default_factory=dict)
    updated_ts: float = 0.0

    def meta(self) -> dict[str, Any]:
        """Snapshot view: metadata only, never content (SPEC-WORKBENCH §9)."""
        return {
            "page_id": self.page_id,
            "type": self.type,
            "ref": self.ref,
            "title": self.title,
            "scope": self.scope,
            "rev": self.rev,
            "pinned": self.pinned,
            "closed": self.closed,
            "session_id": self.session_id,
            "call_name": self.call_name,
            "updated_ts": self.updated_ts,
        }


@dataclass
class Workspace:
    key: str
    host: str
    root: str  # checkout dir (workspace) or cwd (sessionspace)
    name: str  # display: basename of root
    kind: Literal["workspace", "sessionspace"]
    repo: str | None = None
    branch: str | None = None  # display state, refreshed from identity
    common_dir: str | None = None  # rail repo-grouping (worktree siblings)
    pages: dict[str, Page] = field(default_factory=dict)
    findings: dict[str, Finding] = field(default_factory=dict)
    asks: dict[str, Ask] = field(default_factory=dict)

    def pending_review(self) -> list[dict[str, Any]]:
        """Items an agent must act on (SPEC-WORKBENCH §4.2): open findings
        + unanswered asks. Stable ids → idempotent, at-least-once safe.
        Item shape is `{kind: finding|ask, id, workspace, finding|ask}` —
        the payload nests under its kind so item keys can never collide
        with domain keys (a finding has its own `kind`). An item on an
        agent-scoped page carries `agent: <call_name>` — it belongs to
        that page's agent, not the workspace primary (§4.3)."""
        items: list[dict[str, Any]] = []
        for f in self.findings.values():
            if f.status != "open":
                continue
            item: dict[str, Any] = {
                "kind": "finding",
                "id": f.finding_id,
                "workspace": self.key,
                "finding": f.to_dict(),
            }
            page = self.pages.get(f.page_id)
            if page is not None and page.scope == "agent" and page.call_name:
                item["agent"] = page.call_name
            items.append(item)
        for a in self.asks.values():
            if a.answer is None:
                items.append(
                    {
                        "kind": "ask",
                        "id": a.ask_id,
                        "workspace": self.key,
                        "ask": a.to_dict(),
                    }
                )
        return items

    def page_by_ref(self, type_: str, ref: str) -> Page | None:
        for p in self.pages.values():
            if p.type == type_ and p.ref == ref:
                return p
        return None

    def finding_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings.values():
            out[f.status] = out.get(f.status, 0) + 1
        return out

    def meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "host": self.host,
            "root": self.root,
            "name": self.name,
            "kind": self.kind,
            "repo": self.repo,
            "branch": self.branch,
            "common_dir": self.common_dir,
            "pages": [p.meta() for p in self.pages.values()],
            "finding_counts": self.finding_counts(),
        }


def _workspace_key(host: str, root: str) -> str:
    return f"{host}:{root.rstrip('/') or '/'}"


class WorkspaceStore:
    """In-memory store (W0); the manifest layer (W1) hydrates and saves it."""

    def __init__(
        self,
        emit: Callable[[str, dict], object] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._emit = emit or (lambda t, p: None)
        self._now = now
        self._spaces: dict[str, Workspace] = {}
        self._pages_by_id: dict[str, Page] = {}
        self._page_counter = 0

    # ---- resolution (identity -> workspace) --------------------------------

    def resolve(self, identity: dict[str, Any]) -> Workspace:
        """Home for a session: workspace when a checkout root is known
        (identity.worktree, adapter-derived), else sessionspace by cwd.
        Refreshes branch display state; emits workspace.updated on create
        or branch change."""
        host = str(identity.get("host") or "?")
        root = identity.get("worktree")
        if root:
            ws = self._get_or_create(
                host,
                str(root),
                kind="workspace",
                repo=identity.get("repo"),
                common_dir=identity.get("common_dir"),
            )
            branch = identity.get("branch")
            if branch and branch != ws.branch:
                ws.branch = str(branch)
                self._emit_updated(ws)
            if identity.get("common_dir") and not ws.common_dir:
                ws.common_dir = str(identity["common_dir"])
            return ws
        return self._get_or_create(
            host, str(identity.get("cwd") or "?"), kind="sessionspace"
        )

    def home_of(self, identity: dict[str, Any]) -> Workspace | None:
        """Non-mutating resolve: the workspace this identity WOULD land in,
        or None if it doesn't exist yet. Read paths (primary election,
        review-item computation) must never create workspaces or emit."""
        host = str(identity.get("host") or "?")
        root = identity.get("worktree") or identity.get("cwd") or "?"
        return self._spaces.get(_workspace_key(host, str(root)))

    def _get_or_create(
        self,
        host: str,
        root: str,
        *,
        kind: Literal["workspace", "sessionspace"],
        repo: Any = None,
        common_dir: Any = None,
    ) -> Workspace:
        key = _workspace_key(host, root)
        ws = self._spaces.get(key)
        if ws is not None:
            return ws
        ws = Workspace(
            key=key,
            host=host,
            root=root,
            name=root.rstrip("/").split("/")[-1] or root,
            kind=kind,
            repo=str(repo) if repo else None,
            common_dir=str(common_dir) if common_dir else None,
        )
        self._spaces[key] = ws
        self._emit_updated(ws)
        return ws

    def _emit_updated(self, ws: Workspace) -> None:
        self._emit(
            "workspace.updated",
            {
                "key": ws.key,
                "kind": ws.kind,
                "name": ws.name,
                "repo": ws.repo,
                "branch": ws.branch,
                "common_dir": ws.common_dir,
                "pages": len(ws.pages),
            },
        )

    # ---- lookups ------------------------------------------------------------

    def get(self, key: str) -> Workspace | None:
        return self._spaces.get(key)

    def all(self) -> list[Workspace]:
        return list(self._spaces.values())

    def page(self, page_id: str) -> Page | None:
        return self._pages_by_id.get(page_id)

    def workspace_of_page(self, page_id: str) -> Workspace | None:
        for ws in self._spaces.values():
            if page_id in ws.pages:
                return ws
        return None

    # ---- pages ---------------------------------------------------------------

    def _mint_page(self, ws: Workspace, page: Page) -> Page:
        self._page_counter += 1
        page.page_id = f"pg-{self._page_counter}"
        page.updated_ts = self._now()
        ws.pages[page.page_id] = page
        self._pages_by_id[page.page_id] = page
        self._emit_page(ws, page, "added")
        return page

    def _emit_page(self, ws: Workspace, page: Page, action: str) -> None:
        self._emit(
            "page.updated",
            {
                "workspace": ws.key,
                "action": action,
                **page.meta(),
            },
        )

    def upsert_screen(
        self,
        identity: dict[str, Any],
        *,
        session_id: str,
        call_name: str,
        markdown: str,
        title: str | None,
        mode: str,
    ) -> Page:
        """The screen verb as a pinned agent page (SPEC-WORKBENCH §3.2).
        `show` replaces content and bumps rev; `append` grows the current
        rev — mirrors registry.set_screen semantics exactly."""
        ws = self.resolve(identity)
        ref = f"screen:{call_name}"
        page = ws.page_by_ref("screen", ref)
        if page is None:
            page = Page(
                page_id="",
                type="screen",
                ref=ref,
                title=title or f"screen·{call_name}",
                scope="agent",
                pinned=True,
                session_id=session_id,
                call_name=call_name,
                data={"markdown": markdown, "screen_title": title},
            )
            return self._mint_page(ws, page)
        page.session_id = session_id
        if mode == "append":
            page.data["markdown"] = f"{page.data.get('markdown', '')}\n{markdown}"
        else:
            page.data["markdown"] = markdown
            page.data["screen_title"] = title
            page.title = title or page.title
            page.rev += 1
        page.updated_ts = self._now()
        self._emit_page(ws, page, "updated")
        return page

    def push_doc(
        self,
        ws: Workspace,
        *,
        name: str | None = None,
        path: str | None = None,
        content: str | None = None,
    ) -> Page:
        """A doc page: path-backed (server confines + reads fresh) or
        virtual (content held here). Same ref re-push bumps rev."""
        if bool(path) == bool(content is not None):
            raise ValueError("doc needs exactly one of path|content")
        if path:
            ref, title, data = (
                f"doc:{path}",
                name or path.split("/")[-1],
                {"path": path},
            )
        else:
            if not name:
                raise ValueError("virtual doc needs a name")
            if content is None:  # unreachable past the xor guard; narrows type
                raise ValueError("virtual doc needs content")
            ref, title, data = f"doc:{name}", name, {"content": content}
        page = ws.page_by_ref("doc", ref)
        if page is None:
            page = Page(
                page_id="",
                type="doc",
                ref=ref,
                title=title,
                scope="workspace",
                data=data,
            )
            return self._mint_page(ws, page)
        page.data = data
        page.rev += 1
        page.closed = False  # a re-push resurfaces a closed doc
        page.updated_ts = self._now()
        self._emit_page(ws, page, "updated")
        return page

    def upsert_terminal(
        self,
        identity: dict[str, Any],
        *,
        session_id: str,
        call_name: str,
        mode: str,
        handle: str | None = None,
    ) -> Page:
        """A session's terminal page (SPEC-WORKBENCH §5, W4): agent-scoped,
        pinned, one per agent (`term:<call_name>`). `mode` is cell-driven:
        "stream" (pty — live xterm over /v1/term) or "mirror" (tmux —
        read-only capture polling). Re-registration refreshes, no rev bump
        (the terminal itself is the content)."""
        if mode not in ("stream", "mirror"):
            raise ValueError(f"bad terminal mode: {mode}")
        ws = self.resolve(identity)
        ref = f"term:{call_name}"
        data = {"mode": mode, "handle": handle, "call_name": call_name}
        page = ws.page_by_ref("terminal", ref)
        if page is None:
            page = Page(
                page_id="",
                type="terminal",
                ref=ref,
                title=f"term·{call_name}",
                scope="agent",
                pinned=True,
                session_id=session_id,
                call_name=call_name,
                data=data,
            )
            return self._mint_page(ws, page)
        page.session_id = session_id
        page.data = data
        page.updated_ts = self._now()
        self._emit_page(ws, page, "updated")
        return page

    def upsert_diff(
        self,
        ws: Workspace,
        *,
        ref: str,
        title: str,
        files: list[dict],
        source: dict | None,
        diff_key: str | None = None,
    ) -> Page:
        """A diff page (SPEC-WORKBENCH §3.2). `ref` identifies the diff
        (its source signature); re-resolving the same ref bumps rev,
        records the INTERDIFF vs the rev it replaced (W5 — what a
        returning reviewer re-checks), and marks older-rev findings
        stale. `files` is the parsed diff tree (core.diff.parse_diff
        output); `source` is the recorded resolver and `diff_key` the
        content hash, both for live-git tracking."""
        from voco.core.interdiff import compute_interdiff

        page = ws.page_by_ref("diff", ref)
        data: dict[str, Any] = {
            "files": files,
            "source": source,
            "diff_key": diff_key,
        }
        if page is None:
            page = Page(
                page_id="",
                type="diff",
                ref=ref,
                title=title,
                scope="workspace",
                pinned=True,
                data=data,
            )
            return self._mint_page(ws, page)
        data["interdiff"] = compute_interdiff(
            page.data.get("files") or [], files, page.rev
        )
        page.data = data
        page.rev += 1
        page.updated_ts = self._now()
        self._emit_page(ws, page, "updated")
        return page

    def set_closed(self, page_id: str, closed: bool) -> Page:
        page = self._pages_by_id.get(page_id)
        if page is None:
            raise ValueError(f"unknown page: {page_id}")
        if page.pinned and closed:
            raise ValueError("pinned pages cannot be closed")
        ws = self.workspace_of_page(page_id)
        assert ws is not None
        if page.closed != closed:
            page.closed = closed
            page.updated_ts = self._now()
            self._emit_page(ws, page, "updated")
        return page

    # ---- findings ledger (SPEC-WORKBENCH §4) --------------------------------

    def _find(self, workspace_key: str, finding_id: str) -> tuple[Workspace, Finding]:
        ws = self._spaces.get(workspace_key)
        if ws is None or finding_id not in ws.findings:
            raise ValueError(f"unknown finding: {finding_id}")
        return ws, ws.findings[finding_id]

    def _emit_finding(self, action: str, ws: Workspace, f: Finding) -> None:
        # Full state rides the event: last-writer-wins convergence (§4.1).
        self._emit(f"finding.{action}", {"workspace": ws.key, **f.to_dict()})

    def add_finding(
        self,
        workspace_key: str,
        *,
        page_id: str,
        anchor: dict[str, Any],
        text: str,
        kind: str = "concern",
        blocking: bool = False,
    ) -> Finding:
        ws = self._spaces.get(workspace_key)
        if ws is None:
            raise ValueError(f"unknown workspace: {workspace_key}")
        page = ws.pages.get(page_id)
        if page is None:
            raise ValueError(f"finding references unknown page: {page_id}")
        if kind not in ("concern", "question", "nit"):
            raise ValueError(f"bad finding kind: {kind}")
        fid = "f-" + secrets.token_hex(4)
        f = Finding(
            finding_id=fid,
            page_id=page_id,
            rev=page.rev,  # stamped at creation; staleness rides page.rev
            anchor=dict(anchor),
            text=text,
            kind=kind,  # type: ignore[arg-type]
            blocking=bool(blocking),
            created_ts=self._now(),
            updated_ts=self._now(),
        )
        ws.findings[fid] = f
        self._emit_finding("added", ws, f)
        return f

    def update_finding(
        self,
        workspace_key: str,
        finding_id: str,
        *,
        text: str | None = None,
        kind: str | None = None,
        blocking: bool | None = None,
    ) -> Finding:
        """Human edit of the finding body (never status — that is
        finding_status / withdraw)."""
        ws, f = self._find(workspace_key, finding_id)
        if text is not None:
            f.text = text
        if kind is not None:
            if kind not in ("concern", "question", "nit"):
                raise ValueError(f"bad finding kind: {kind}")
            f.kind = kind  # type: ignore[assignment]
        if blocking is not None:
            f.blocking = bool(blocking)
        f.updated_ts = self._now()
        self._emit_finding("updated", ws, f)
        return f

    def set_finding_status(
        self,
        workspace_key: str,
        finding_id: str,
        status: str,
        *,
        note: str | None = None,
        commit: str | None = None,
        answer: str | None = None,
        agent: bool = False,
    ) -> Finding:
        """Status round-trip. Agents (agent=True) may set only
        addressed/disputed/wont-fix; the human may also open/withdraw."""
        ws, f = self._find(workspace_key, finding_id)
        valid = (
            AGENT_STATUSES
            if agent
            else {"open", "addressed", "disputed", "wont-fix", "withdrawn"}
        )
        if status not in valid:
            raise ValueError(f"status {status!r} not allowed here")
        # Withdraw is the human's final word: an agent with a stale id cannot
        # resurrect a withdrawn finding (review WARNING 5).
        if agent and f.status == "withdrawn":
            raise ValueError("finding withdrawn; agents cannot change it")
        # At-least-once means agents replay reports: an exact duplicate is
        # a true no-op — no ts bump, no event (§4.2).
        if (
            status == f.status
            and (note is None or note == f.note)
            and (commit is None or commit == f.commit)
            and (answer is None or answer == f.answer)
        ):
            return f
        f.status = status  # type: ignore[assignment]
        if note is not None:
            f.note = note
        if commit is not None:
            f.commit = commit
        if answer is not None:
            f.answer = answer
        f.updated_ts = self._now()
        self._emit_finding("updated", ws, f)
        return f

    def withdraw_finding(self, workspace_key: str, finding_id: str) -> Finding:
        return self.set_finding_status(workspace_key, finding_id, "withdrawn")

    def findings_for(
        self, workspace_key: str, *, open_only: bool = False
    ) -> list[dict]:
        ws = self._spaces.get(workspace_key)
        if ws is None:
            return []
        out = [f.to_dict() for f in ws.findings.values()]
        if open_only:
            out = [f for f in out if f["status"] == "open"]
        return out

    def asks_for(self, workspace_key: str, *, open_only: bool = False) -> list[dict]:
        """Asks mirror findings_for: open ≡ unanswered."""
        ws = self._spaces.get(workspace_key)
        if ws is None:
            return []
        out = [a.to_dict() for a in ws.asks.values()]
        if open_only:
            out = [a for a in out if a["answer"] is None]
        return out

    # ---- asks (SPEC-WORKBENCH §4.3) -----------------------------------------

    def add_ask(
        self, workspace_key: str, *, text: str, context: dict | None = None
    ) -> Ask:
        ws = self._spaces.get(workspace_key)
        if ws is None:
            raise ValueError(f"unknown workspace: {workspace_key}")
        aid = "a-" + secrets.token_hex(4)
        a = Ask(ask_id=aid, text=text, context=context, created_ts=self._now())
        ws.asks[aid] = a
        self._emit("ask.created", {"workspace": ws.key, **a.to_dict()})
        return a

    def answer_ask(self, workspace_key: str, ask_id: str, markdown: str) -> Ask:
        ws = self._spaces.get(workspace_key)
        if ws is None or ask_id not in ws.asks:
            raise ValueError(f"unknown ask: {ask_id}")
        a = ws.asks[ask_id]
        if a.answer == markdown:  # at-least-once replay: true no-op (§4.2)
            return a
        a.answer = markdown
        a.answered_ts = self._now()
        self._emit("ask.answered", {"workspace": ws.key, **a.to_dict()})
        return a

    def answer_finding(
        self, workspace_key: str, finding_id: str, markdown: str
    ) -> Finding:
        """An agent answers a question-kind finding in place (§4.2). For a
        question the reply IS the round-trip, so an open question flips to
        addressed — at-least-once redelivery must converge without a
        separate finding_status call. Other kinds keep their status.
        An exact-duplicate reply (replay) is a true no-op."""
        ws, f = self._find(workspace_key, finding_id)
        if f.answer == markdown and not (f.kind == "question" and f.status == "open"):
            return f
        f.answer = markdown
        if f.kind == "question" and f.status == "open":
            f.status = "addressed"
        f.updated_ts = self._now()
        self._emit_finding("updated", ws, f)
        return f

    # ---- persistence (SPEC-WORKBENCH §8; the manifest adapter drives fs) -----

    MANIFEST_VERSION = 1

    def dump_workspace(self, ws: Workspace) -> dict:
        """Full persistable state for one workspace. Pages persist by
        content (virtual docs, diffs) or by path (path-docs re-read fresh);
        screen pages persist their markdown so a restart keeps the board."""
        return {
            "v": self.MANIFEST_VERSION,
            "key": ws.key,
            "host": ws.host,
            "root": ws.root,
            "name": ws.name,
            "kind": ws.kind,
            "repo": ws.repo,
            "branch": ws.branch,
            "common_dir": ws.common_dir,
            "page_counter": self._page_counter,
            "pages": [{**p.meta(), "data": p.data} for p in ws.pages.values()],
            "findings": [f.to_dict() for f in ws.findings.values()],
            "asks": [a.to_dict() for a in ws.asks.values()],
        }

    def restore_workspace(self, data: dict) -> Workspace | None:
        """Rebuild one workspace from a manifest. Defensive: a malformed
        entry is skipped, never fatal (losing one workspace beats a boot
        refusal)."""
        if not isinstance(data, dict) or data.get("v") != self.MANIFEST_VERSION:
            return None
        # Build fully local first; register in _spaces/_pages_by_id only on
        # success — a malformed entry mid-parse must not leave orphaned
        # page ids pointing at a workspace that was never added.
        try:
            ws = Workspace(
                key=str(data["key"]),
                host=str(data["host"]),
                root=str(data["root"]),
                name=str(data["name"]),
                kind=data["kind"],
                repo=data.get("repo"),
                branch=data.get("branch"),
                common_dir=data.get("common_dir"),
            )
            for praw in data.get("pages", []):
                page = Page(
                    page_id=str(praw["page_id"]),
                    type=str(praw["type"]),
                    ref=str(praw["ref"]),
                    title=str(praw["title"]),
                    scope=praw["scope"],
                    rev=int(praw.get("rev", 1)),
                    pinned=bool(praw.get("pinned", False)),
                    closed=bool(praw.get("closed", False)),
                    session_id=praw.get("session_id"),
                    call_name=praw.get("call_name"),
                    data=dict(praw.get("data", {})),
                    updated_ts=float(praw.get("updated_ts", 0.0)),
                )
                ws.pages[page.page_id] = page
            for fraw in data.get("findings", []):
                f = Finding(
                    finding_id=str(fraw["finding_id"]),
                    page_id=str(fraw["page_id"]),
                    rev=int(fraw.get("rev", 1)),
                    anchor=dict(fraw.get("anchor", {})),
                    text=str(fraw.get("text", "")),
                    kind=fraw.get("kind", "concern"),
                    blocking=bool(fraw.get("blocking", False)),
                    status=fraw.get("status", "open"),
                    note=fraw.get("note"),
                    commit=fraw.get("commit"),
                    answer=fraw.get("answer"),
                    created_ts=float(fraw.get("created_ts", 0.0)),
                    updated_ts=float(fraw.get("updated_ts", 0.0)),
                )
                ws.findings[f.finding_id] = f
            for araw in data.get("asks", []):
                a = Ask(
                    ask_id=str(araw["ask_id"]),
                    text=str(araw.get("text", "")),
                    context=araw.get("context"),
                    answer=araw.get("answer"),
                    created_ts=float(araw.get("created_ts", 0.0)),
                    answered_ts=float(araw.get("answered_ts", 0.0)),
                )
                ws.asks[a.ask_id] = a
        except (KeyError, TypeError, ValueError):
            return None
        self._spaces[ws.key] = ws
        self._pages_by_id.update(ws.pages)
        counter = data.get("page_counter", 0)
        if isinstance(counter, int) and counter > self._page_counter:
            self._page_counter = counter
        return ws

    def dirty_keys(self) -> list[str]:
        return list(self._spaces.keys())

    # ---- snapshot (SPEC-WORKBENCH §9) ---------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        return [ws.meta() for ws in self._spaces.values()]
