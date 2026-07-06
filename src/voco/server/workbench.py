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
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from voco.core.workspace import Page, Workspace
    from voco.server.http import BridgeServer

STATIC_DIR = Path(__file__).parent / "static"
MAX_DOC_BYTES = 2 * 1024 * 1024

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


def confined_read(root: str, path: str) -> str:
    """Read `path` only if it resolves inside `root`. Runs on EVERY read —
    a symlink swapped after push fails the next read's check."""
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise web.HTTPNotFound(text="outside workspace root")
    if not resolved.is_file():
        raise web.HTTPNotFound(text="no such doc")
    if resolved.stat().st_size > MAX_DOC_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_DOC_BYTES, actual_size=resolved.stat().st_size
        )
    data = resolved.read_bytes()
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
        if type_ != "doc":
            # diff push lands in W1; keep the refusal soft and honest.
            raise web.HTTPBadRequest(text=f"page type {type_!r} not accepted yet")
        if ws.kind == "sessionspace":
            raise web.HTTPBadRequest(
                text="no repo detected for this session; docs need a workspace"
            )
        path = body.get("path")
        if path:
            if s.identity.get("host") != self._host:
                # local_fs capability cell: the daemon cannot read a
                # remote disk — the adapter resolves there, pushes content.
                raise web.HTTPBadRequest(
                    text="remote session: push content instead of a path"
                )
            confined_read(ws.root, str(path))  # confine + readable, up front
            page = store.push_doc(ws, name=body.get("name"), path=str(path))
        else:
            try:
                page = store.push_doc(
                    ws, name=body.get("name"), content=body.get("content")
                )
            except ValueError as e:
                raise web.HTTPBadRequest(text=str(e)) from e
        return web.json_response({"ok": True, "page_id": page.page_id, "rev": page.rev})
