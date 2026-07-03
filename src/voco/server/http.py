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
from typing import TYPE_CHECKING, Callable

from aiohttp import WSMsgType, web

from voco.protocol.messages import CommandReply, validate_envelope

if TYPE_CHECKING:
    from voco.core.events import EventBus
    from voco.core.registry import Registry

LISTEN_SLICE_S = 50.0


class BridgeServer:
    def __init__(
        self,
        registry: Registry,
        bus: EventBus,
        *,
        token: str | None = None,
        listen_slice_s: float = LISTEN_SLICE_S,
        on_control: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._token = token
        self._slice = listen_slice_s
        # Daemon-owned control commands (mic.set, interrupt, switch...).
        self._on_control = on_control or (lambda cmd, payload: {})
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
        return app

    def _check_auth(self, request: web.Request) -> None:
        if self._token is None:
            return
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {self._token}":
            raise web.HTTPUnauthorized(text="bad token")

    async def _register(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        body = await request.json()
        identity = {
            k: body.get(k)
            for k in ("host", "user", "cwd", "repo", "branch", "worktree", "harness", "pid")
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
        except asyncio.TimeoutError:
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
            raise web.HTTPBadRequest(text=str(e))
        if env.type == "state.get":
            return web.json_response(self._registry.snapshot())
        result = self._on_control(env.type, env.payload)
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
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                await ws.send_json(self._handle_ws_command(msg.data))
        finally:
            unsubscribe()
            sender.cancel()
        return ws

    async def _pump_ws(
        self, ws: web.WebSocketResponse, queue: asyncio.Queue[str]
    ) -> None:
        while True:
            await ws.send_str(await queue.get())

    def _handle_ws_command(self, raw: str) -> dict:
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
            result = self._on_control(env.type, env.payload)
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
