"""Bridge + control + WS surfaces (SPEC §2, §8.1, §10; SPEC-WORKBENCH §8.5).

ROLE: the localhost HTTP/WS server. Thin over core: every handler is
translate-validate-delegate; long-poll parking (newest-poll-wins) lives
here because futures are transport, not domain state.

INVARIANTS: binds 127.0.0.1 only (SPEC §8.1); optional bearer token checked
on every /v1/bridge route when configured; listen returns within
listen_slice_s; a new listen for a session completes the old one with
`rearm` (review finding 13); WS clients get a `snapshot` first.
Browser defense (SPEC-WORKBENCH §8.5): loopback binding does not protect
against hostile pages in the user's own browser — every mutating route and
WS upgrade rejects foreign Origins (loopback origins pass on any port;
no-Origin clients like curl/adapters pass), and browser-originated
mutations additionally require the per-run workbench token, which only the
pages this daemon serves receive.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from voco.protocol.messages import CommandReply, validate_envelope

if TYPE_CHECKING:
    from voco.core.events import EventBus
    from voco.core.registry import Registry
    from voco.core.workspace import WorkspaceStore

LISTEN_SLICE_S = 50.0
WS_EVENT_QUEUE_SIZE = 512
WS_COMMAND_QUEUE_SIZE = 64

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# The identity facts an adapter may assert — at register and, since the
# 2026-07-06 stale-root dogfood failure, re-asserted on every workspace
# verb (the register-time snapshot must never outlive the adapter's
# current truth).
IDENTITY_FIELDS = (
    "host",
    "user",
    "cwd",
    "repo",
    "branch",
    "worktree",
    "common_dir",
    "harness",
    "pid",
    "instance",
    # Transport facts that unlock capabilities (derive-don't-ask):
    # a tmux pane/session enables inject; host_alias routes it.
    "tmux_pane",
    "tmux_session",
    "host_alias",
)


async def _no_control(cmd: str, payload: dict) -> dict:
    return {}


@web.middleware
async def error_middleware(request: web.Request, handler):
    """No bare 500s (dogfood 2026-07-06): agents act on error bodies, and
    a blank 500 sent one flailing through six blind retries. HTTPException
    subclasses are deliberate replies and pass through; anything else
    becomes a 500 with the exception named in a JSON body."""
    try:
        return await handler(request)
    except (web.HTTPException, asyncio.CancelledError):
        raise
    except Exception as e:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            raise  # can't send a fresh response on an upgraded socket
        return web.json_response(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
        )


def identity_from_query(request: web.Request) -> dict | None:
    """GET verbs carry re-asserted identity as a JSON query param."""
    raw = request.query.get("identity")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class BridgeServer:
    def __init__(
        self,
        registry: Registry,
        bus: EventBus,
        *,
        token: str | None = None,
        listen_slice_s: float = LISTEN_SLICE_S,
        on_control: Callable[[str, dict], Awaitable[dict]] | None = None,
        snapshot_extra: Callable[[], dict] | None = None,
        workspaces: WorkspaceStore | None = None,
        allowed_origins: list[str] | None = None,
        health_info: Callable[[], dict] | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._token = token
        self._slice = listen_slice_s
        self.workspaces = workspaces
        # P4: live daemon facts merged into /v1/health (version, uptime,
        # voice/floor state). None = the bare service signature only.
        self._health_info = health_info
        # §8.5: per-run workbench token — reaches browsers only inside the
        # pages this daemon serves; cross-origin pages cannot read it (no
        # CORS headers on any response), so holding it proves the page is
        # ours. Required for browser-originated mutations and WS commands.
        self.wb_token = secrets.token_hex(16)
        self._allowed_origins = {o.rstrip("/") for o in (allowed_origins or [])}
        # Diff resolution runs git/gh in a workspace root (SPEC-WORKBENCH
        # §3.2); constructed lazily so a review-only daemon with no git in
        # PATH still boots.
        from voco.adapters.diffsource import DiffResolver

        self.diff_resolver = DiffResolver()
        # W4: session_id -> live PtyProcess (or None). The daemon injects
        # its lookup; the default means "no streaming terminals here"
        # (tests, headless bridges).
        self.pty_lookup: Callable[[str], Any] = lambda sid: None
        # B1b: url-mode artifacts iframe arbitrary origins — daemon wires
        # this from [workbench] allow_artifact_urls; default off.
        self.allow_artifact_urls: bool = False
        # Daemon-owned control commands (mic.set, interrupt, switch...).
        # Async so subprocess-backed commands (tmux/ssh) never block the
        # loop that pumps WS events and listen polls.
        self._on_control = on_control or _no_control
        # One mutation lane across HTTP and every WS client. Control commands
        # change shared routing/audio/workbench state and must not race merely
        # because two tabs or an MCP client acted at once.
        self._control_lock = asyncio.Lock()
        # Daemon-owned live state (mic duplex/attention) merged into every
        # snapshot so UIs render current truth without waiting for events.
        self._snapshot_extra = snapshot_extra
        # session_id -> (parked future, poller id). The poller id lets a
        # NEW listener supersede an old one exactly once instead of the
        # two ping-ponging rearm evictions forever (live-test spam bug).
        self._waiters: dict[str, tuple[asyncio.Future[dict], str]] = {}
        registry.try_deliver = self._try_deliver

    # ---- delivery port for the registry -----------------------------------

    def _snapshot(self) -> dict:
        snap = self._registry.snapshot()
        if self.workspaces is not None:
            # SPEC-WORKBENCH §9: page metadata, never content.
            snap["workspaces"] = self.workspaces.snapshot()
        if self._snapshot_extra is not None:
            snap.update(self._snapshot_extra())
        return snap

    def _try_deliver(self, session_id: str, payload: dict) -> bool:
        entry = self._waiters.pop(session_id, None)
        if entry is not None and not entry[0].done():
            entry[0].set_result(payload)
            return True
        return False

    # ---- app ----------------------------------------------------------------

    def build_app(self) -> web.Application:
        from voco.server.workbench import add_workbench_routes

        app = web.Application(middlewares=[error_middleware])
        app.router.add_post("/v1/bridge/register", self._register)
        app.router.add_post("/v1/bridge/say", self._say)
        app.router.add_post("/v1/bridge/screen", self._screen)
        app.router.add_get("/v1/bridge/listen", self._listen)
        app.router.add_post("/v1/control/{cmd}", self._control)
        app.router.add_get("/v1/events", self._events_ws)
        app.router.add_get("/v1/health", self._health)
        app.router.add_get("/debug", self._ui)
        app.router.add_get("/ui", self._ui)  # legacy alias for the debug UI
        add_workbench_routes(app, self)  # `/`, /static/*, page reads, page push
        return app

    def _check_auth(self, request: web.Request) -> None:
        if self._token is None:
            return
        header = request.headers.get("Authorization", "")
        if header == f"Bearer {self._token}":
            return
        # Browsers cannot set headers on a WebSocket: the UI passes the
        # token as ?token= instead (loopback-only surface; SPEC §8.1).
        if request.query.get("token") == self._token:
            return
        raise web.HTTPUnauthorized(text="bad token")

    # ---- §8.5 browser defense ------------------------------------------------

    def _origin_ok(self, origin: str) -> bool:
        if origin.rstrip("/") in self._allowed_origins:
            return True
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            return False
        return host in LOOPBACK_HOSTS

    def _check_origin(self, request: web.Request) -> None:
        """Reject foreign browser origins. No Origin header (curl, the
        adapters, the CLI) passes — browsers always send one."""
        origin = request.headers.get("Origin")
        if origin is not None and not self._origin_ok(origin):
            raise web.HTTPForbidden(text="foreign origin")

    def _wb_ok(self, request: web.Request) -> bool:
        supplied = request.headers.get("x-voco-wb") or request.query.get("wb")
        return supplied == self.wb_token

    def _check_browser_mutation(self, request: web.Request) -> None:
        """Origin discipline plus, for browser-originated requests, the
        workbench token — a rogue loopback-served page passes the origin
        check but can never read our token (CORS blocks the read)."""
        self._check_origin(request)
        if request.headers.get("Origin") is not None and not self._wb_ok(request):
            raise web.HTTPForbidden(text="workbench token required")

    # ---- debug UI (protocol reference client at /debug) ----------------------

    def html_response(self, body: str) -> web.Response:
        """Serve HTML under the workbench CSP (SPEC-WORKBENCH §7): only
        our own modules and same-origin/WS connections; inline scripts run
        only with the per-response nonce this method injects for the
        `{{nonce}}` placeholder."""
        nonce = secrets.token_hex(8)
        body = body.replace("{{nonce}}", nonce)
        resp = web.Response(text=body, content_type="text/html")
        # connect-src 'self' covers same-origin ws:// under CSP3, so we do
        # NOT open ws:/wss: globally — that would let an XSS exfiltrate over
        # a WebSocket to any host (review NOTE 11).
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'"
        )
        return resp

    async def _ui(self, request: web.Request) -> web.Response:
        page = Path(__file__).with_name("ui.html")
        if not page.exists():
            raise web.HTTPNotFound(text="ui.html missing from install")
        body = page.read_text(encoding="utf-8")
        # The debug UI issues WS commands too — hand it the wb token the
        # same way the workbench gets it (§8.5: in-page only).
        inject = (
            f'<script nonce="{{{{nonce}}}}">'
            f'window.__VOCO_WB__="{self.wb_token}";</script>'
        )
        return self.html_response(body.replace("<body>", f"<body>{inject}", 1))

    async def _health(self, request: web.Request) -> web.Response:
        """P4: the lifecycle health probe. Unauthenticated GET, loopback
        bind, no mutation — `voco up` polls it with no token, and the
        `service` field is the signature that a random HTTP listener
        squatting the port cannot accidentally fake."""
        info: dict[str, Any] = dict(self._health_info()) if self._health_info else {}
        # the signature outranks the callback: healthy() trusts these
        # two keys, so no info payload may ever shadow them
        info["service"] = "voco-d"
        info["ok"] = True
        return web.json_response(info)

    async def _register(self, request: web.Request) -> web.Response:
        self._check_browser_mutation(request)
        self._check_auth(request)
        body = await request.json()
        identity = {k: body.get(k) for k in IDENTITY_FIELDS}
        if not identity.get("host") or not identity.get("cwd"):
            raise web.HTTPBadRequest(text="host and cwd are required")
        caps = body.get("capabilities") or ["say", "listen"]
        s = self._registry.register(identity, caps)
        if self.workspaces is not None:
            # A session's workspace exists the moment it registers — the
            # rail must show every attached agent, not only the ones that
            # already pushed a page (live-test bug: register-only agents
            # were invisible).
            self.workspaces.resolve(s.identity)
        return web.json_response(
            {
                "session_id": s.session_id,
                "call_name": s.call_name,
                "display_name": s.display_name,
            }
        )

    def _session_or_410(self, request: web.Request, session_id: str | None):
        if not session_id:
            raise web.HTTPBadRequest(text="session_id required")
        s = self._registry.get(session_id)
        if s is None:
            # 410 tells adapters to re-register (daemon restarted).
            raise web.HTTPGone(text="unknown session; re-register")
        return s

    def refresh_session_identity(self, s, supplied: object):
        """Staleness kill (dogfood 2026-07-06): adapters re-assert their
        current identity on workspace verbs; the session's register-time
        snapshot yields to it, so verbs resolve against the LIVE root.
        Malformed or absent identity leaves the session untouched."""
        if not isinstance(supplied, dict):
            return s
        ident = {k: supplied.get(k) for k in IDENTITY_FIELDS if k in supplied}
        if not ident.get("host") or not ident.get("cwd"):
            return s
        return self._registry.refresh_identity(s.session_id, ident) or s

    async def _say(self, request: web.Request) -> web.Response:
        self._check_browser_mutation(request)
        self._check_auth(request)
        body = await request.json()
        s = self._session_or_410(request, body.get("session_id"))
        s = self.refresh_session_identity(s, body.get("identity"))
        text = str(body.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="text required")
        self._registry.record_say(s.session_id, text, body.get("turn_id"))
        return web.json_response({"ok": True})

    async def _screen(self, request: web.Request) -> web.Response:
        self._check_browser_mutation(request)
        self._check_auth(request)
        body = await request.json()
        s = self._session_or_410(request, body.get("session_id"))
        s = self.refresh_session_identity(s, body.get("identity"))
        mode = body.get("mode", "show")
        if mode not in ("show", "append"):
            raise web.HTTPBadRequest(text="mode must be show|append")
        markdown = str(body.get("markdown") or "")
        title = body.get("title")
        self._registry.set_screen(s.session_id, markdown, title, mode)
        if self.workspaces is not None:
            # The screen verb doubles as the pinned screen page
            # (SPEC-WORKBENCH §3.2); wire compat above is untouched.
            self.workspaces.upsert_screen(
                s.identity,
                session_id=s.session_id,
                call_name=s.call_name,
                markdown=markdown,
                title=title,
                mode=mode,
            )
        return web.json_response({"ok": True})

    async def _listen(self, request: web.Request) -> web.Response:
        self._check_origin(request)
        self._check_auth(request)
        session_id = request.query.get("session_id", "")
        # A detached session answers "detach", never 410: a listener that
        # missed the live delivery must stop, not re-register a session
        # the user just ended (live-test resurrection bug).
        if session_id and self._registry.was_detached(session_id):
            return web.json_response({"status": "detach", "reason": "detached"})
        s = self._session_or_410(request, session_id)
        # Review-item computation routes by workspace — a parked agent
        # whose registered root went stale would never be woken for its
        # real workspace, so listen re-asserts identity too.
        s = self.refresh_session_identity(s, identity_from_query(request))
        poller = request.query.get("poller", "")
        # Newest-poll-wins. The SAME poller re-arming gets a rearm (its
        # slice loop continues); a DIFFERENT poller supersedes the old
        # one, which must stop instead of fighting back.
        old = self._waiters.pop(s.session_id, None)
        if old is not None and not old[0].done():
            status = "rearm" if poller == old[1] else "superseded"
            old[0].set_result({"status": status})
        immediate = self._registry.on_listen_start(s.session_id)
        if immediate is not None:
            return web.json_response(immediate)
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._waiters[s.session_id] = (fut, poller)
        try:
            payload = await asyncio.wait_for(fut, timeout=self._slice)
        except TimeoutError:
            payload = {"status": "rearm"}
        finally:
            # Only the current owner may unpark: an evicted poll's cleanup
            # must not clobber the newer poll's parked state, and a
            # delivered poll was already unparked by dispatch.
            entry = self._waiters.get(s.session_id)
            if entry is not None and entry[0] is fut:
                del self._waiters[s.session_id]
                self._registry.on_listen_end(s.session_id)
        return web.json_response(payload)

    async def shutdown(self) -> None:
        for fut, _poller in self._waiters.values():
            if not fut.done():
                fut.set_result({"status": "detach", "reason": "shutdown"})
        self._waiters.clear()

    # ---- control ---------------------------------------------------------------

    async def _control(self, request: web.Request) -> web.Response:
        self._check_browser_mutation(request)
        self._check_auth(request)
        cmd = request.match_info["cmd"]
        body = {}
        if request.can_read_body:
            body = await request.json()
        try:
            env = validate_envelope({"cmd": cmd, "payload": body})
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e)) from e
        if env.type == "state.get":
            return web.json_response(self._snapshot())
        try:
            async with self._control_lock:
                result = await self._on_control(env.type, env.payload)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            # Adapter failures (tmux missing, ssh down) are operator errors,
            # not crashes: surface the message, keep the daemon calm.
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        return web.json_response({"ok": True, **result})

    # ---- WS events (SPEC §10) -----------------------------------------------------

    async def _events_ws(self, request: web.Request) -> web.WebSocketResponse:
        self._check_origin(request)  # §8.5: refuse foreign-origin upgrades
        self._check_auth(request)
        # WebSockets are exempt from CORS, so a browser-origin connection
        # must present the workbench token to even READ the event stream —
        # the snapshot carries session cwds/screens/finding data a hostile
        # loopback-served page must not siphon (review BLOCKER 1). A
        # no-Origin connection (CLI tools, curl) follows the bearer policy
        # only, exactly like the HTTP surfaces.
        browser = request.headers.get("Origin") is not None
        if browser and not self._wb_ok(request):
            raise web.HTTPForbidden(text="workbench token required")
        commands_ok = not browser or self._wb_ok(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WS_EVENT_QUEUE_SIZE)
        command_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WS_COMMAND_QUEUE_SIZE)
        overflowed = False
        close_task: asyncio.Task[bool] | None = None

        def push_raw(raw: str) -> None:
            nonlocal overflowed, close_task
            if overflowed:
                return
            try:
                queue.put_nowait(raw)
            except asyncio.QueueFull:
                # A client that missed one event cannot safely continue from
                # partial state. Close it explicitly; reconnect always starts
                # with an authoritative snapshot.
                overflowed = True
                close_task = asyncio.create_task(
                    ws.close(code=1013, message=b"event backlog; resync")
                )

        def push(env) -> None:
            push_raw(json.dumps(env.to_dict()))

        unsubscribe = self._bus.subscribe(
            lambda env: loop.call_soon_threadsafe(push, env)
        )
        # Per-connection snapshot (SPEC §10): stamped but not broadcast.
        push(self._bus.make("snapshot", self._snapshot()))
        sender = asyncio.create_task(self._pump_ws(ws, queue))

        # One bounded worker per connection prevents a fast/buggy client from
        # creating unbounded tasks. The global control lock above serializes
        # this worker with HTTP and other clients.
        async def command_worker() -> None:
            while True:
                raw = await command_queue.get()
                reply = await self._handle_ws_command(raw, commands_ok=commands_ok)
                push_raw(json.dumps(reply))

        worker = asyncio.create_task(command_worker())

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    command_queue.put_nowait(msg.data)
                except asyncio.QueueFull:
                    try:
                        req_id = json.loads(msg.data).get("id")
                    except (ValueError, AttributeError):
                        req_id = None
                    push_raw(
                        json.dumps(
                            CommandReply(
                                id=req_id,
                                ok=False,
                                error="too many pending commands",
                            ).to_dict()
                        )
                    )
        finally:
            unsubscribe()
            sender.cancel()
            worker.cancel()
            await asyncio.gather(sender, worker, return_exceptions=True)
            if close_task is not None:
                await asyncio.gather(close_task, return_exceptions=True)
        return ws

    async def _pump_ws(
        self, ws: web.WebSocketResponse, queue: asyncio.Queue[str]
    ) -> None:
        while True:
            await ws.send_str(await queue.get())

    async def _handle_ws_command(self, raw: str, *, commands_ok: bool = True) -> dict:
        try:
            data = json.loads(raw)
            req_id = data.get("id")
            env = validate_envelope(data)
        except (json.JSONDecodeError, ValueError) as e:
            return CommandReply(id=None, ok=False, error=str(e)).to_dict()
        if not commands_ok:
            return CommandReply(
                id=req_id, ok=False, error="workbench token required"
            ).to_dict()
        if env.type == "state.get":
            return CommandReply(id=req_id, ok=True, payload=self._snapshot()).to_dict()
        try:
            async with self._control_lock:
                result = await self._on_control(env.type, env.payload)
            return CommandReply(id=req_id, ok=True, payload=result).to_dict()
        except Exception as e:
            return CommandReply(id=req_id, ok=False, error=str(e)).to_dict()


async def run_server(
    server: BridgeServer, host: str = "127.0.0.1", port: int = 7777
) -> web.AppRunner:
    if host != "127.0.0.1":
        raise ValueError("SPEC §8.1: bridge binds loopback only")
    # 2s shutdown: cleanup() otherwise waits up to 60s for lingering
    # connections — an OPEN BROWSER TAB's events WebSocket held the
    # manifest lock hostage past the restart retry (2026-07-08, fifth
    # strike). Loopback WS clients self-heal by design; cut them fast.
    runner = web.AppRunner(server.build_app(), shutdown_timeout=2.0)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
