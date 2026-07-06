"""Workbench HTTP surface — shell, static client, page content, page push
(SPEC-WORKBENCH §3, §7–§9).

ROLE: the browser-facing half of the workbench. Serves the buildless
client at `/`, resolves page content on demand (docs read fresh from
disk, confined to the workspace root and re-checked on every read —
that is the TOCTOU stance), and implements the `page` bridge verb.

INVARIANTS: path-backed docs never escape the workspace root (realpath
containment per read; symlinks resolve before the check); docs are
served by page id, never by client-supplied path; content responses are
size-capped and binary-refused; sessionspaces take no review pages;
`path`/`source` pushes require a same-host session (`local_fs` cell —
remote adapters resolve locally and push content instead).
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from voco.core.workspace import Page, Workspace
    from voco.server.http import BridgeServer

STATIC_DIR = Path(__file__).parent / "static"
MAX_DOC_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 8 * 1024 * 1024  # a pasted patch cap (review W8)

SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voco deck</title>
<link rel="stylesheet" href="/static/styles.css">
</head>
<body>
<div id="app"></div>
<script nonce="{{nonce}}">window.__VOCO__ = {cfg};</script>
<script type="module" src="/static/app.mjs"></script>
</body>
</html>
"""


def add_workbench_routes(app: web.Application, server: BridgeServer) -> None:
    wb = WorkbenchRoutes(server)
    app.router.add_get("/", wb.index)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", STATIC_DIR, follow_symlinks=False)
    app.router.add_get("/v1/page/{page_id}", wb.page_content)
    app.router.add_post("/v1/bridge/page", wb.bridge_page)
    app.router.add_get("/v1/bridge/findings", wb.bridge_findings)
    app.router.add_post("/v1/bridge/finding_status", wb.bridge_finding_status)
    app.router.add_post("/v1/bridge/ask_reply", wb.bridge_ask_reply)


def handle_workbench_command(store, cmd: str, payload: dict, *, data_dir):
    """Workbench control commands (SPEC-WORKBENCH §9) — pure store ops, no
    audio/tmux. Shared by the daemon's control dispatch and tests. Returns a
    reply dict, or raises KeyError for a non-workbench command (the caller
    then tries its own handlers) / ValueError for a bad request."""
    if cmd == "workspace.list":
        return {"workspaces": store.snapshot()}
    if cmd == "page.close":
        return {"page": store.set_closed(str(payload.get("page_id", "")), True).meta()}
    if cmd == "page.reopen":
        return {"page": store.set_closed(str(payload.get("page_id", "")), False).meta()}
    if cmd == "finding.list":
        return {"findings": store.findings_for(str(payload.get("workspace", "")))}
    if cmd == "finding.add":
        f = store.add_finding(
            str(payload.get("workspace", "")),
            page_id=str(payload.get("page_id", "")),
            anchor=payload.get("anchor") or {},
            text=str(payload.get("text", "")),
            kind=str(payload.get("kind", "concern")),
            blocking=bool(payload.get("blocking", False)),
        )
        return {"finding": f.to_dict()}
    if cmd == "finding.update":
        f = store.update_finding(
            str(payload.get("workspace", "")),
            str(payload.get("finding_id", "")),
            text=payload.get("text"),
            kind=payload.get("kind"),
            blocking=payload.get("blocking"),
        )
        return {"finding": f.to_dict()}
    if cmd == "finding.withdraw":
        f = store.withdraw_finding(
            str(payload.get("workspace", "")), str(payload.get("finding_id", ""))
        )
        return {"finding": f.to_dict()}
    if cmd == "ask.create":
        a = store.add_ask(
            str(payload.get("workspace", "")),
            text=str(payload.get("text", "")),
            context=payload.get("context"),
        )
        return {"ask": a.to_dict()}
    if cmd == "ask.list":
        return {"asks": store.asks_for(str(payload.get("workspace", "")))}
    if cmd == "review.export":
        from voco.core.review_export import export_workspace

        return export_workspace(
            store,
            str(payload.get("workspace", "")),
            out=payload.get("out"),
            data_dir=data_dir,
        )
    raise KeyError(cmd)


def confined_read(root: str, path: str) -> str:
    """Read `path` only if it resolves inside `root`, fd-based so the caps
    apply to the exact bytes returned (review BLOCKER 2). A relative `path`
    resolves against the workspace root, never the daemon cwd (review W10).
    Re-checked on EVERY read — a symlink swapped after push fails next read.
    """
    root_resolved = Path(root).resolve()
    base = Path(path)
    if not base.is_absolute():
        base = root_resolved / base
    resolved = base.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise web.HTTPNotFound(text="outside workspace root")
    # Open the RESOLVED path (no further symlink) and read from the fd, so
    # size/binary checks and the returned bytes are the same object — a
    # post-check swap cannot substitute different content.
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise web.HTTPNotFound(text="no such doc") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise web.HTTPNotFound(text="not a regular file")
        if st.st_size > MAX_DOC_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_DOC_BYTES, actual_size=st.st_size
            )
        data = os.read(fd, MAX_DOC_BYTES + 1)
    finally:
        os.close(fd)
    if b"\0" in data[:8192]:
        raise web.HTTPUnsupportedMediaType(text="binary file")
    return data.decode("utf-8", errors="replace")


class WorkbenchRoutes:
    def __init__(self, server: BridgeServer) -> None:
        self._server = server
        # Short name — the adapters register `hostname.split(".")[0]`.
        self._host = socket.gethostname().split(".")[0]

    # ---- shell ---------------------------------------------------------------

    async def index(self, request: web.Request) -> web.Response:
        cfg = {"wb": self._server.wb_token}
        body = SHELL.replace("{cfg}", json.dumps(cfg))
        return self._server.html_response(body)

    # ---- page content (read on demand; snapshot carries metadata only) --------

    async def page_content(self, request: web.Request) -> web.Response:
        store = self._server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        page = store.page(request.match_info["page_id"])
        if page is None or page.closed:
            raise web.HTTPNotFound(text="no such page")
        ws = store.workspace_of_page(page.page_id)
        assert ws is not None
        return web.json_response(
            {"page": page.meta(), "content": self._content(ws, page)}
        )

    def _content(self, ws: Workspace, page: Page) -> dict:
        if page.type == "screen":
            return {
                "markdown": page.data.get("markdown", ""),
                "title": page.data.get("screen_title"),
            }
        if page.type == "doc":
            if "content" in page.data:
                return {"markdown": page.data["content"]}
            return {"markdown": confined_read(ws.root, page.data["path"])}
        if page.type == "diff":
            return {
                "files": page.data.get("files", []),
                "source": page.data.get("source"),
            }
        raise web.HTTPNotImplemented(text=f"page type {page.type} lands later")

    # ---- the page bridge verb (SPEC-WORKBENCH §3.2) ---------------------------

    async def bridge_page(self, request: web.Request) -> web.Response:
        server = self._server
        server._check_browser_mutation(request)
        server._check_auth(request)
        store = server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        body = await request.json()
        s = server._session_or_410(request, body.get("session_id"))
        ws = store.resolve(s.identity)
        type_ = body.get("type")
        if type_ not in ("doc", "diff"):
            raise web.HTTPBadRequest(text=f"page type {type_!r} not accepted")
        if ws.kind == "sessionspace":
            raise web.HTTPBadRequest(
                text="no repo for this session; review pages need a workspace"
            )
        if type_ == "diff":
            page = await self._push_diff(ws, s, body)
        else:
            page = self._push_doc(ws, s, body)
        return web.json_response({"ok": True, "page_id": page.page_id, "rev": page.rev})

    def _push_doc(self, ws, s, body):
        store = self._server.workspaces
        assert store is not None  # bridge_page guarded it
        path = body.get("path")
        if path:
            if s.identity.get("host") != self._host:
                # local_fs capability cell: the daemon cannot read a
                # remote disk — the adapter resolves there, pushes content.
                raise web.HTTPBadRequest(
                    text="remote session: push content instead of a path"
                )
            confined_read(ws.root, str(path))  # confine + readable, up front
            return store.push_doc(ws, name=body.get("name"), path=str(path))
        try:
            return store.push_doc(
                ws, name=body.get("name"), content=body.get("content")
            )
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e

    async def _push_diff(self, ws, s, body):
        import asyncio

        from voco.adapters.diffsource import DiffResolveError, source_ref
        from voco.core.diff import parse_diff

        store = self._server.workspaces
        assert store is not None  # bridge_page guarded it
        content = body.get("content")
        source = body.get("source")
        if content is not None:
            # A raw patch: never live-tracked (no recorded source). Cap it —
            # a huge paste must not exhaust memory or stall the loop (W8).
            text = str(content)
            if len(text) > MAX_DIFF_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_DIFF_BYTES, actual_size=len(text)
                )
            ref = body.get("name") or "diff:pasted"
            diff_text, recorded, title = text, None, body.get("name") or "diff"
        elif isinstance(source, dict):
            if s.identity.get("host") != self._host:
                raise web.HTTPBadRequest(
                    text="remote session: resolve locally and push diff content"
                )
            if "diff_file" in source:
                # Read confined and USE that content — never re-open the path
                # unconfined in the resolver (review BLOCKER 3).
                diff_text = confined_read(ws.root, str(source["diff_file"]))
            else:
                loop = asyncio.get_running_loop()
                try:
                    diff_text = await loop.run_in_executor(
                        None, self._server.diff_resolver.resolve, source, ws.root
                    )
                except DiffResolveError as e:
                    raise web.HTTPBadRequest(text=str(e)) from e
            ref, recorded, title = source_ref(source), source, source_ref(source)
        else:
            raise web.HTTPBadRequest(text="diff needs `source` or `content`")
        files = parse_diff(diff_text)
        return store.upsert_diff(ws, ref=ref, title=title, files=files, source=recorded)

    # ---- findings bridge verbs (SPEC-WORKBENCH §4.1) -------------------------

    async def bridge_findings(self, request: web.Request) -> web.Response:
        """An agent reads its workspace's ledger. Authorization: a session
        sees ONLY its own workspace's findings (§4.1)."""
        server = self._server
        server._check_origin(request)
        server._check_auth(request)
        store = server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        s = server._session_or_410(request, request.query.get("session_id"))
        ws = store.resolve(s.identity)
        open_only = request.query.get("pending") in ("1", "true")
        return web.json_response(
            {
                "workspace": ws.key,
                "findings": store.findings_for(ws.key, open_only=open_only),
                "asks": store.asks_for(ws.key, open_only=open_only),
            }
        )

    async def bridge_finding_status(self, request: web.Request) -> web.Response:
        """An agent reports a round-trip status (addressed/disputed/wont-fix)."""
        server = self._server
        server._check_browser_mutation(request)
        server._check_auth(request)
        store = server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        body = await request.json()
        s = server._session_or_410(request, body.get("session_id"))
        ws = store.resolve(s.identity)
        fid = str(body.get("finding_id", ""))
        if fid not in ws.findings:
            # A session may only touch its OWN workspace's ledger (§4.1).
            raise web.HTTPNotFound(text="finding not in this session's workspace")
        try:
            f = store.set_finding_status(
                ws.key,
                fid,
                str(body.get("status", "")),
                note=body.get("note"),
                commit=body.get("commit"),
                agent=True,
            )
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e
        return web.json_response({"ok": True, "finding": f.to_dict()})

    async def bridge_ask_reply(self, request: web.Request) -> web.Response:
        """An agent answers an ask (chat question) or a question-kind finding
        in markdown. `ask_id` targets an ask; `finding_id` a finding."""
        server = self._server
        server._check_browser_mutation(request)
        server._check_auth(request)
        store = server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        body = await request.json()
        s = server._session_or_410(request, body.get("session_id"))
        ws = store.resolve(s.identity)
        markdown = str(body.get("markdown", ""))
        try:
            if body.get("ask_id"):
                if str(body["ask_id"]) not in ws.asks:
                    raise web.HTTPNotFound(text="ask not in this session's workspace")
                a = store.answer_ask(ws.key, str(body["ask_id"]), markdown)
                return web.json_response({"ok": True, "ask": a.to_dict()})
            if body.get("finding_id"):
                if str(body["finding_id"]) not in ws.findings:
                    raise web.HTTPNotFound(text="finding not in this workspace")
                f = store.answer_finding(ws.key, str(body["finding_id"]), markdown)
                return web.json_response({"ok": True, "finding": f.to_dict()})
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e
        raise web.HTTPBadRequest(text="ask_reply needs ask_id or finding_id")
