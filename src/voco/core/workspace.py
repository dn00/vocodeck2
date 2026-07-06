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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

PageScope = Literal["workspace", "agent"]

# Page types shipped in W0/W1; the set is data, additive by design.
PAGE_TYPES = {"screen", "terminal", "diff", "doc"}
PINNED_TYPES = {"screen", "terminal", "diff"}


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

    def page_by_ref(self, type_: str, ref: str) -> Page | None:
        for p in self.pages.values():
            if p.type == type_ and p.ref == ref:
                return p
        return None

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

    # ---- snapshot (SPEC-WORKBENCH §9) ---------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        return [ws.meta() for ws in self._spaces.values()]
