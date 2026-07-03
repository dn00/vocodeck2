"""Bridge + control + WS surfaces (SPEC §2, §8.1, §10).

ROLE: the localhost HTTP/WS server. Thin over core: every handler is
translate-validate-delegate; long-poll parking (newest-poll-wins) lives
here because futures are transport, not domain state.

INVARIANTS: binds 127.0.0.1 only (SPEC §8.1); optional bearer token checked
on every /v1/bridge route when configured; listen returns within
listen_slice_s; a new listen for a session completes the old one with
`rearm` (review finding 13); WS clients get a `snapshot` first.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from voco.protocol.messages import CommandReply, validate_envelope

if TYPE_CHECKING:
    from voco.core.events import EventBus
    from voco.core.registry import Registry

LISTEN_SLICE_S = 50.0


async def _no_control(cmd: str, payload: dict) -> dict:
    return {}


class BridgeServer:
    def __init__(
        self,
        registry: Registry,
        bus: EventBus,
        *,
        token: str | None = None,
        listen_slice_s: float = LISTEN_SLICE_S,
        on_control: Callable[[str, dict], Awaitable[dict]] | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._token = token
        self._slice = listen_slice_s
        # Daemon-owned control commands (mic.set, interrupt, switch...).
        # Async so subprocess-backed commands (tmux/ssh) never block the
        # loop that pumps WS events and listen polls.
        self._on_control = on_control or _no_control
        self._waiters: dict[str, asyncio.Future[dict]] = {}
        registry.try_deliver = self._try_deliver

    # ---- delivery port for the registry -----------------------------------

    def _try_deliver(self, session_id: str, payload: dict) -> bool:
        fut = self._waiters.pop(session_id, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)
            return True
        return False

    # ---- app ----------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/bridge/register", self._register)
        app.router.add_post("/v1/bridge/say", self._say)
        app.router.add_post("/v1/bridge/screen", self._screen)
        app.router.add_get("/v1/bridge/listen", self._listen)
        app.router.add_post("/v1/control/{cmd}", self._control)
        app.router.add_get("/v1/events", self._events_ws)
        app.router.add_get("/", self._ui)
        app.router.add_get("/ui", self._ui)
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

    # ---- debug UI (self-contained page; no secrets, so no auth) -------------

    async def _ui(self, request: web.Request) -> web.Response:
        page = Path(__file__).with_name("ui.html")
        if not page.exists():
            raise web.HTTPNotFound(text="ui.html missing from install")
        return web.Response(
            text=page.read_text(encoding="utf-8"), content_type="text/html"
        )

    async def _register(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        body = await request.json()
        identity = {
            k: body.get(k)
            for k in (
                "host",
                "user",
                "cwd",
                "repo",
                "branch",
                "worktree",
                "harness",
                "pid",
                # Transport facts that unlock capabilities (derive-don't-ask):
                # a tmux pane/session enables inject; host_alias routes it.
                "tmux_pane",
                "tmux_session",
                "host_alias",
            )
        }
        if not identity.get("host") or not identity.get("cwd"):
            raise web.HTTPBadRequest(text="host and cwd are required")
        caps = body.get("capabilities") or ["say", "listen"]
        s = self._registry.register(identity, caps)
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

    async def _say(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        body = await request.json()
        s = self._session_or_410(request, body.get("session_id"))
        text = str(body.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="text required")
        self._registry.record_say(s.session_id, text, body.get("turn_id"))
        return web.json_response({"ok": True})

    async def _screen(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        body = await request.json()
        s = self._session_or_410(request, body.get("session_id"))
        mode = body.get("mode", "show")
        if mode not in ("show", "append"):
            raise web.HTTPBadRequest(text="mode must be show|append")
        self._registry.set_screen(
            s.session_id, str(body.get("markdown") or ""), body.get("title"), mode
        )
        return web.json_response({"ok": True})

    async def _listen(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        session_id = request.query.get("session_id", "")
        s = self._session_or_410(request, session_id)
        # Newest-poll-wins: evict any parked poll with a rearm.
        old = self._waiters.pop(s.session_id, None)
        if old is not None and not old.done():
            old.set_result({"status": "rearm"})
        immediate = self._registry.on_listen_start(s.session_id)
        if immediate is not None:
            return web.json_response(immediate)
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._waiters[s.session_id] = fut
        try:
            payload = await asyncio.wait_for(fut, timeout=self._slice)
        except TimeoutError:
            payload = {"status": "rearm"}
        finally:
            # Only the current owner may unpark: an evicted poll's cleanup
            # must not clobber the newer poll's parked state, and a
            # delivered poll was already unparked by dispatch.
            if self._waiters.get(s.session_id) is fut:
                del self._waiters[s.session_id]
                self._registry.on_listen_end(s.session_id)
        return web.json_response(payload)

    async def shutdown(self) -> None:
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_result({"status": "detach"})
        self._waiters.clear()

    # ---- control ---------------------------------------------------------------

    async def _control(self, request: web.Request) -> web.Response:
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
            return web.json_response(self._registry.snapshot())
        try:
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
        self._check_auth(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)

        def push(env) -> None:
            try:
                queue.put_nowait(json.dumps(env.to_dict()))
            except asyncio.QueueFull:
                pass  # named fail-silent: a slow UI drops events, never
                # blocks the daemon; it can re-sync from a fresh snapshot.

        unsubscribe = self._bus.subscribe(
            lambda env: loop.call_soon_threadsafe(push, env)
        )
        # Per-connection snapshot (SPEC §10): stamped but not broadcast.
        push(self._bus.make("snapshot", self._registry.snapshot()))
        sender = asyncio.create_task(self._pump_ws(ws, queue))
        # Commands run as tasks and reply through the event queue: the
        # receive loop stays responsive during a slow peek/spawn, and one
        # task (the pump) is the only writer — no interleaved WS frames.
        pending: set[asyncio.Task[None]] = set()

        async def run_command(raw: str) -> None:
            reply = await self._handle_ws_command(raw)
            try:
                queue.put_nowait(json.dumps(reply))
            except asyncio.QueueFull:
                pass  # same named fail-silent as events: slow UI drops

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                task = asyncio.create_task(run_command(msg.data))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            unsubscribe()
            sender.cancel()
            for task in pending:
                task.cancel()
        return ws

    async def _pump_ws(
        self, ws: web.WebSocketResponse, queue: asyncio.Queue[str]
    ) -> None:
        while True:
            await ws.send_str(await queue.get())

    async def _handle_ws_command(self, raw: str) -> dict:
        try:
            data = json.loads(raw)
            req_id = data.get("id")
            env = validate_envelope(data)
        except (json.JSONDecodeError, ValueError) as e:
            return CommandReply(id=None, ok=False, error=str(e)).to_dict()
        if env.type == "state.get":
            return CommandReply(
                id=req_id, ok=True, payload=self._registry.snapshot()
            ).to_dict()
        try:
            result = await self._on_control(env.type, env.payload)
            return CommandReply(id=req_id, ok=True, payload=result).to_dict()
        except Exception as e:
            return CommandReply(id=req_id, ok=False, error=str(e)).to_dict()


async def run_server(
    server: BridgeServer, host: str = "127.0.0.1", port: int = 7777
) -> web.AppRunner:
    if host != "127.0.0.1":
        raise ValueError("SPEC §8.1: bridge binds loopback only")
    runner = web.AppRunner(server.build_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
