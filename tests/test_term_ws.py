"""W4 — terminal pages, capability cells, and the /v1/term stream.

Daemon level: pty spawn/kill lifecycle, terminal-page upsert on register,
cells in the snapshot, pty peek. HTTP level: the /v1/term WS end-to-end
against a REAL pty (replay → live output → input → resize → close), and
its browser-auth gate."""

from __future__ import annotations

import asyncio
import json
import socket
import sys

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from voco.daemon import Daemon
from voco.server.http import BridgeServer

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="pty backend is Unix-only in v1"
)

HOST = socket.gethostname().split(".")[0]


@pytest.fixture
def daemon() -> Daemon:
    d = Daemon({}, no_audio=True)
    d._wire_terminal_pages()  # run() does this; tests wire it explicitly
    return d


def pty_ident(handle: str, cwd: str = "/repo/a") -> dict:
    return {
        "host": HOST,
        "cwd": cwd,
        "worktree": cwd,
        "harness": "claude",
        "instance": handle,
    }


# ---- daemon lifecycle -------------------------------------------------------


async def test_spawn_pty_backend_and_kill(daemon):
    result = await daemon._control(
        "session.spawn", {"harness": "cat", "backend": "pty"}
    )
    assert result["backend"] == "pty"
    handle = result["term"]
    assert handle.startswith("pty-")
    assert daemon._pty is not None and daemon._pty.get(handle).alive
    await daemon._control("session.kill", {"name": handle})
    assert daemon._pty.get(handle) is None


async def test_spawn_pty_rejects_host(daemon):
    with pytest.raises(ValueError, match="local-only"):
        await daemon._control(
            "session.spawn", {"harness": "cat", "backend": "pty", "host": "ws"}
        )


async def test_spawn_rejects_unknown_backend(daemon):
    with pytest.raises(ValueError, match=r"tmux\|pty"):
        await daemon._control("session.spawn", {"harness": "cat", "backend": "screen"})


async def test_config_default_backend(tmp_path):
    d = Daemon({"terminal": {"default_backend": "pty"}}, no_audio=True)
    result = await d._control("session.spawn", {"harness": "cat"})
    assert result["backend"] == "pty"
    d._pty.shutdown()


async def test_pty_register_creates_stream_page_and_cells(daemon):
    spawn = await daemon._control("session.spawn", {"harness": "cat", "backend": "pty"})
    s = daemon.registry.register(pty_ident(spawn["term"]), ["say", "listen"])
    assert "inject" in s.capabilities  # a daemon pty is an inject transport

    ws = daemon.workspaces.home_of(s.identity)
    page = ws.page_by_ref("terminal", f"term:{s.call_name}")
    assert page is not None and page.pinned and page.scope == "agent"
    assert page.data["mode"] == "stream"
    assert page.data["handle"] == spawn["term"]

    snap = daemon.registry.snapshot()["sessions"][0]
    assert snap["term"]["backend"] == "pty"
    assert snap["term"]["stream"] is True
    assert snap["term"]["survives_restart"] is False
    daemon._pty.shutdown()


async def test_tmux_register_creates_mirror_page_and_cells(daemon):
    s = daemon.registry.register(
        {
            "host": HOST,
            "cwd": "/repo/a",
            "worktree": "/repo/a",
            "harness": "claude",
            "tmux_pane": "%3",
        },
        ["say"],
    )
    ws = daemon.workspaces.home_of(s.identity)
    page = ws.page_by_ref("terminal", f"term:{s.call_name}")
    assert page is not None and page.data["mode"] == "mirror"
    snap = daemon.registry.snapshot()["sessions"][0]
    assert snap["term"]["backend"] == "tmux"
    assert snap["term"]["stream"] is False
    assert snap["term"]["survives_restart"] is True


async def test_plain_session_has_no_terminal(daemon):
    s = daemon.registry.register(
        {"host": HOST, "cwd": "/x", "harness": "codex"}, ["say"]
    )
    assert daemon.registry.snapshot()["sessions"][0]["term"] is None
    ws = daemon.workspaces.home_of(s.identity)
    assert ws is None or ws.page_by_ref("terminal", f"term:{s.call_name}") is None


async def test_peek_answers_from_pty_ring(daemon):
    spawn = await daemon._control(
        "session.spawn", {"harness": "echo pty-peek-check; cat", "backend": "pty"}
    )
    s = daemon.registry.register(pty_ident(spawn["term"]), ["say"])
    for _ in range(100):
        result = await daemon._control("session.peek", {"name": s.call_name})
        if "pty-peek-check" in result["text"]:
            break
        await asyncio.sleep(0.02)
    assert "pty-peek-check" in result["text"]
    daemon._pty.shutdown()


# ---- /v1/term over real HTTP --------------------------------------------------


@pytest.fixture
async def client(daemon):
    server = BridgeServer(
        daemon.registry,
        daemon.bus,
        listen_slice_s=0.5,
        workspaces=daemon.workspaces,
    )
    server.pty_lookup = daemon._pty_lookup
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    c.daemon = daemon
    yield c
    await c.close()
    if daemon._pty is not None:
        daemon._pty.shutdown()


async def spawn_and_register(daemon) -> tuple[str, str]:
    spawn = await daemon._control(
        "session.spawn", {"harness": "echo replay-me; cat", "backend": "pty"}
    )
    s = daemon.registry.register(pty_ident(spawn["term"]), ["say", "listen"])
    return s.session_id, spawn["term"]


async def test_term_ws_replay_io_and_close(client):
    sid, handle = await spawn_and_register(client.daemon)
    pp = client.daemon._pty.get(handle)
    for _ in range(100):  # wait for the echo to land in the ring
        if b"replay-me" in pp.replay():
            break
        await asyncio.sleep(0.02)

    ws = await client.ws_connect(f"/v1/term/{sid}")
    msg = await ws.receive(timeout=5)
    assert msg.type == aiohttp.WSMsgType.BINARY
    assert b"replay-me" in msg.data  # scrollback replays first

    await ws.send_str(json.dumps({"resize": {"cols": 100, "rows": 30}}))
    await ws.send_bytes(b"typed-live\n")
    got = b""
    while b"typed-live" not in got:
        msg = await ws.receive(timeout=5)
        assert msg.type == aiohttp.WSMsgType.BINARY
        got += msg.data

    client.daemon._pty.kill(handle)  # daemon-side death closes the socket
    while True:
        msg = await ws.receive(timeout=5)
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            break


async def test_term_ws_requires_wb_token_for_browsers(client):
    sid, _handle = await spawn_and_register(client.daemon)
    resp = await client.get(
        f"/v1/term/{sid}", headers={"Origin": "http://127.0.0.1:7777"}
    )
    assert resp.status == 403  # loopback origin but no workbench token
    resp = await client.get(
        f"/v1/term/{sid}", headers={"Origin": "https://evil.example"}
    )
    assert resp.status == 403  # foreign origin, flat refusal


async def test_term_ws_404_without_streaming_terminal(client):
    s = client.daemon.registry.register(
        {"host": HOST, "cwd": "/x", "harness": "codex"}, ["say"]
    )
    resp = await client.get(f"/v1/term/{s.session_id}")
    assert resp.status == 404
