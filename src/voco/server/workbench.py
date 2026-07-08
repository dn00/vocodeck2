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

import asyncio
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

        async def no_cache(request: web.Request, response: web.StreamResponse) -> None:
            # Without this, browsers heuristic-cache the client modules and
            # an open tab keeps running WEEKS-old UI after a rebuild (live
            # report 2026-07-07). no-cache = always revalidate; the ETags
            # aiohttp already sends make that a cheap 304.
            if request.path.startswith("/static/") or request.path == "/":
                response.headers["Cache-Control"] = "no-cache"

        app.on_response_prepare.append(no_cache)
    app.router.add_get("/v1/page/{page_id}", wb.page_content)
    app.router.add_get("/v1/file", wb.file_content)
    app.router.add_get("/v1/term/{session_id}", wb.term_ws)
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
    if cmd == "finding.status":
        # The HUMAN status path (agent=False → open/withdrawn allowed too).
        # Exists for undo-over-confirm: withdraw's undo re-opens (U2c).
        f = store.set_finding_status(
            str(payload.get("workspace", "")),
            str(payload.get("finding_id", "")),
            str(payload.get("status", "")),
            note=payload.get("note"),
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


def diff_fingerprint(text: str) -> str:
    """Content identity for a resolved diff (live-git change detection)."""
    import hashlib

    return hashlib.sha1(text.encode()).hexdigest()[:12]


def confined_read(root: str, path: str) -> str:
    """Read `path` only if it resolves inside `root`, fd-based so the caps
    apply to the exact bytes returned (review BLOCKER 2). A relative `path`
    resolves against the workspace root, never the daemon cwd (review W10).
    Re-checked on EVERY read — a symlink swapped after push fails next read.
    Refusals name the attempted path AND the root (dogfood 2026-07-06: a
    bare "no such doc" sent an agent through six blind retries).
    """
    root_resolved = Path(root).resolve()
    base = Path(path)
    if not base.is_absolute():
        base = root_resolved / base
    resolved = base.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise web.HTTPNotFound(
            text=f"{path!r} is outside the workspace root {str(root_resolved)!r}"
        )
    # Open the RESOLVED path (no further symlink) and read from the fd, so
    # size/binary checks and the returned bytes are the same object — a
    # post-check swap cannot substitute different content.
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise web.HTTPNotFound(
            text=(
                f"no such doc: {path!r} (looked for {str(resolved)!r}; "
                f"workspace root {str(root_resolved)!r})"
            )
        ) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise web.HTTPNotFound(text=f"not a regular file: {str(resolved)!r}")
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

    async def file_content(self, request: web.Request) -> web.Response:
        """B1c file viewer: read ONE tracked file, confined to the
        workspace root (same fd-based confinement + size/binary caps as
        doc pages). Browser origins need the wb token, like every other
        workbench surface."""
        self._server._check_browser_mutation(request)
        store = self._server.workspaces
        if store is None:
            raise web.HTTPNotFound(text="workbench disabled")
        ws = store.get(str(request.query.get("workspace", "")))
        if ws is None:
            raise web.HTTPNotFound(
                text=f"unknown workspace: {request.query.get('workspace')!r}"
            )
        if ws.kind == "sessionspace":
            raise web.HTTPBadRequest(text=f"{ws.key} has no checkout")
        path = str(request.query.get("path", ""))
        if not path or path.startswith("-"):
            raise web.HTTPBadRequest(text="path required")
        # The viewer's contract is TRACKED files (xai B1c blocker): the
        # list came from `git ls-files`, so reads verify the same way —
        # confinement alone would still expose untracked secrets (.env,
        # logs) and .git internals under the root.
        from voco.adapters.diffsource import _default_runner

        root = ws.root
        tracked = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _default_runner(
                ["git", "ls-files", "--error-unmatch", "--", path], root
            ),
        )
        if tracked.returncode != 0:
            raise web.HTTPNotFound(text=f"not a tracked file: {path!r}")
        content = await asyncio.get_running_loop().run_in_executor(
            None, confined_read, ws.root, path
        )
        return web.json_response({"path": path, "content": content})

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
            params = page.data.get("params") or {}
            if "content" in page.data:
                return {"markdown": page.data["content"], "params": params}
            return {
                "markdown": confined_read(ws.root, page.data["path"]),
                "params": params,
            }
        if page.type == "diff":
            return {
                "files": page.data.get("files", []),
                "source": page.data.get("source"),
                # W5: what moved since the rev this one replaced — the
                # client's since-rev banner + per-file chips.
                "interdiff": page.data.get("interdiff"),
            }
        if page.type == "terminal":
            # The page carries HOW to attach (SPEC-WORKBENCH §5): stream →
            # /v1/term/{session_id} WS; mirror → poll session.peek. `handle`
            # (pty-N / tmux session) is the killable unit for the head
            # action — only daemon-spawned terminals have one.
            return {
                "mode": page.data.get("mode"),
                "call_name": page.data.get("call_name"),
                "session_id": page.session_id,
                "handle": page.data.get("handle"),
            }
        raise web.HTTPNotImplemented(text=f"page type {page.type} lands later")

    # ---- live terminal stream (SPEC-WORKBENCH §5, W4) --------------------------

    async def term_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Binary frames: pty output (out) / keyboard input (in). Text
        frames: JSON control ({"resize": {cols, rows}}). On open, the
        scrollback ring replays as the first frame; the ring — not the
        client queue — is the recovery source. A mutating surface: Origin
        discipline + wb token for browsers + bearer when configured."""
        import asyncio
        import json as _json

        from voco.adapters.ptyterm import PtyError

        server = self._server
        server._check_origin(request)
        server._check_auth(request)
        if request.headers.get("Origin") is not None and not server._wb_ok(request):
            raise web.HTTPForbidden(text="workbench token required")
        pp = server.pty_lookup(request.match_info["session_id"])
        if pp is None:
            raise web.HTTPNotFound(text="no streaming terminal for this session")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # Subscribe THEN snapshot, back-to-back with no await between:
        # a frame arriving after this pair lands in the queue only, one
        # arriving before is in the replay only — no loss, no duplicate.
        queue = pp.subscribe()
        replay = pp.replay()
        if replay:
            await ws.send_bytes(replay)

        async def pump() -> None:
            while True:
                frame = await queue.get()
                if frame is None:
                    await ws.close(message=b"terminal exited")
                    return
                await ws.send_bytes(frame)

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    try:
                        pp.write(msg.data)
                    except (PtyError, OSError):
                        break  # terminal died mid-type; pump closes us
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        control = _json.loads(msg.data)
                    except ValueError:
                        continue
                    size = control.get("resize")
                    if isinstance(size, dict):
                        pp.resize(
                            int(size.get("cols") or 80), int(size.get("rows") or 24)
                        )
        finally:
            pp.unsubscribe(queue)
            sender.cancel()
        return ws

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
        s = server.refresh_session_identity(s, body.get("identity"))
        ws = store.resolve(s.identity)
        type_ = body.get("type")
        if type_ not in ("doc", "diff"):
            raise web.HTTPBadRequest(text=f"page type {type_!r} not accepted")
        if ws.kind == "sessionspace":
            raise web.HTTPBadRequest(
                text=(
                    f"no git checkout known for this session "
                    f"(cwd {s.identity.get('cwd')!r}); review pages need a workspace"
                )
            )
        if type_ == "diff":
            page = await self._push_diff(ws, s, body)
        else:
            page = self._push_doc(ws, s, body)
        return web.json_response(
            {
                "ok": True,
                "page_id": page.page_id,
                "rev": page.rev,
                "workspace": ws.key,
                "root": ws.root,
            }
        )

    def _push_doc(self, ws, s, body):
        store = self._server.workspaces
        assert store is not None  # bridge_page guarded it
        params = self._doc_params(body)
        path = body.get("path")
        if path:
            if s.identity.get("host") != self._host:
                # local_fs capability cell: the daemon cannot read a
                # remote disk — the adapter resolves there, pushes content.
                raise web.HTTPBadRequest(
                    text=(
                        f"remote session (host {s.identity.get('host')!r}, "
                        f"daemon on {self._host!r}): push content instead of a path"
                    )
                )
            confined_read(ws.root, str(path))  # confine + readable, up front
            return store.push_doc(
                ws, name=body.get("name"), path=str(path), params=params
            )
        try:
            return store.push_doc(
                ws, name=body.get("name"), content=body.get("content"), params=params
            )
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e

    @staticmethod
    def _doc_params(body) -> dict | None:
        """B1a, per the reference's PAGE-TYPES contract: params are a
        strictly whitelisted capability set — an unknown key is a 400
        NAMING the known set, so the contract stays legible to agents.
        None (absent) on a re-push keeps the page's existing params."""
        params = body.get("params")
        if params is None:
            return None
        if not isinstance(params, dict):
            raise web.HTTPBadRequest(text="params must be an object")
        unknown = set(params) - {"annotatable"}
        if unknown:
            raise web.HTTPBadRequest(
                text=f"unknown params {sorted(unknown)}; known: ['annotatable']"
            )
        if "annotatable" in params and not isinstance(params["annotatable"], bool):
            raise web.HTTPBadRequest(text="annotatable must be a boolean")
        return params

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
                    text=(
                        f"remote session (host {s.identity.get('host')!r}, "
                        f"daemon on {self._host!r}): resolve locally and "
                        "push diff content"
                    )
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
                    # Name the root git actually ran in — "git failed" with
                    # no place was undebuggable in the dogfood.
                    raise web.HTTPBadRequest(
                        text=f"{e} (workspace root {ws.root!r})"
                    ) from e
                if len(diff_text) > MAX_DIFF_BYTES:
                    # Same cap as pasted content: a monster branch/PR diff
                    # must not stall the daemon or bloat page state.
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_DIFF_BYTES, actual_size=len(diff_text)
                    )
            ref, recorded, title = source_ref(source), source, source_ref(source)
        else:
            raise web.HTTPBadRequest(text="diff needs `source` or `content`")
        files = parse_diff(diff_text)
        return store.upsert_diff(
            ws,
            ref=ref,
            title=title,
            files=files,
            source=recorded,
            diff_key=diff_fingerprint(diff_text),
        )

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
        from voco.server.http import identity_from_query

        s = server._session_or_410(request, request.query.get("session_id"))
        s = server.refresh_session_identity(s, identity_from_query(request))
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
        s = server.refresh_session_identity(s, body.get("identity"))
        ws = store.resolve(s.identity)
        fid = str(body.get("finding_id", ""))
        if fid not in ws.findings:
            # A session may only touch its OWN workspace's ledger (§4.1).
            raise web.HTTPNotFound(
                text=f"finding {fid!r} not in this session's workspace ({ws.key})"
            )
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
        s = server.refresh_session_identity(s, body.get("identity"))
        ws = store.resolve(s.identity)
        markdown = str(body.get("markdown", ""))
        try:
            if body.get("ask_id"):
                if str(body["ask_id"]) not in ws.asks:
                    raise web.HTTPNotFound(
                        text=(
                            f"ask {body['ask_id']!r} not in this session's "
                            f"workspace ({ws.key})"
                        )
                    )
                a = store.answer_ask(ws.key, str(body["ask_id"]), markdown)
                return web.json_response({"ok": True, "ask": a.to_dict()})
            if body.get("finding_id"):
                if str(body["finding_id"]) not in ws.findings:
                    raise web.HTTPNotFound(
                        text=(
                            f"finding {body['finding_id']!r} not in this "
                            f"session's workspace ({ws.key})"
                        )
                    )
                f = store.answer_finding(ws.key, str(body["finding_id"]), markdown)
                return web.json_response({"ok": True, "finding": f.to_dict()})
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e
        raise web.HTTPBadRequest(text="ask_reply needs ask_id or finding_id")
