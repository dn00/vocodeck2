"""Bridge integration tests (SPEC §8.1) — real aiohttp server, ephemeral port."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from voco.core.events import EventBus
from voco.core.registry import Registry
from voco.server.http import BridgeServer


@pytest.fixture
async def client():
    bus = EventBus()
    registry = Registry(emit=bus.emit)
    server = BridgeServer(registry, bus, listen_slice_s=0.5)
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    c.registry = registry
    c.bus = bus
    yield c
    await c.close()


IDENT = {"host": "mac", "user": "dn", "cwd": "/repo/a", "harness": "claude"}


async def register(client) -> dict:
    resp = await client.post("/v1/bridge/register", json=IDENT)
    assert resp.status == 200
    return await resp.json()


async def test_register_say_and_dispatch_roundtrip(client):
    info = await register(client)
    assert info["call_name"]
    sid = info["session_id"]

    # Park a listen, then dispatch through the registry (as the daemon would).
    listen = asyncio.create_task(_listen_json(client, sid))
    await asyncio.sleep(0.05)  # let the poll park
    result = client.registry.dispatch("run the tests", client.registry.mint_turn_id())
    assert result == "live"
    payload = await listen
    assert payload["status"] == "transcript"
    assert payload["text"] == "run the tests"
    assert payload["turn_id"].startswith("t-")

    # say while active
    resp = await client.post(
        "/v1/bridge/say", json={"session_id": sid, "text": "tests passed"}
    )
    assert (await resp.json())["ok"] is True


async def _listen_json(client, sid: str) -> dict:
    resp = await client.get(f"/v1/bridge/listen?session_id={sid}")
    return await resp.json()


async def test_listen_rearm_on_slice_expiry(client):
    info = await register(client)
    payload = await _listen_json(client, info["session_id"])
    assert payload == {"status": "rearm"}


async def test_newest_poll_wins(client):
    info = await register(client)
    sid = info["session_id"]
    first = asyncio.create_task(_listen_json(client, sid))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(_listen_json(client, sid))
    await asyncio.sleep(0.05)
    assert (await first) == {"status": "rearm"}  # evicted immediately
    client.registry.dispatch("hello", client.registry.mint_turn_id())
    payload = await second
    assert payload["status"] == "transcript" and payload["text"] == "hello"


async def test_queued_input_delivered_on_next_listen(client):
    info = await register(client)
    sid = info["session_id"]
    # Not parked: dispatch queues (idle).
    assert (
        client.registry.dispatch("first", client.registry.mint_turn_id())
        == "queued_idle"
    )
    payload = await _listen_json(client, sid)
    assert payload["status"] == "transcript" and payload["text"] == "first"


async def test_detach_unparks_listener_with_detach_status(client):
    info = await register(client)
    sid = info["session_id"]
    listen = asyncio.create_task(_listen_json(client, sid))
    await asyncio.sleep(0.05)  # let the poll park
    client.registry.detach(sid)
    payload = await listen
    assert payload == {"status": "detach"}  # clean exit, not a slice timeout


async def test_unknown_session_is_410(client):
    resp = await client.get("/v1/bridge/listen?session_id=deadbeef")
    assert resp.status == 410
    resp = await client.post(
        "/v1/bridge/say", json={"session_id": "deadbeef", "text": "hi"}
    )
    assert resp.status == 410


async def test_screen_verb_stores_and_state_endpoint_reads(client):
    info = await register(client)
    sid = info["session_id"]
    resp = await client.post(
        "/v1/bridge/screen",
        json={"session_id": sid, "markdown": "# plan", "title": "Plan", "mode": "show"},
    )
    assert resp.status == 200
    state = await (await client.post("/v1/control/state.get", json={})).json()
    assert state["sessions"][0]["screen_title"] == "Plan"


async def test_bearer_token_enforced_when_configured():
    bus = EventBus()
    registry = Registry(emit=bus.emit)
    server = BridgeServer(registry, bus, token="sekrit", listen_slice_s=0.2)
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    try:
        resp = await c.post("/v1/bridge/register", json=IDENT)
        assert resp.status == 401
        resp = await c.post(
            "/v1/bridge/register",
            json=IDENT,
            headers={"Authorization": "Bearer sekrit"},
        )
        assert resp.status == 200
    finally:
        await c.close()


async def test_register_with_tmux_pane_gets_inject_capability(client):
    resp = await client.post(
        "/v1/bridge/register", json={**IDENT, "cwd": "/repo/tm", "tmux_pane": "%3"}
    )
    assert resp.status == 200
    state = await (await client.post("/v1/control/state.get", json={})).json()
    s = next(x for x in state["sessions"] if "tm" in x["display_name"])
    assert "inject" in s["capabilities"]  # would be dropped if the bridge
    # didn't forward tmux_pane into identity (regression: live-smoke find)


async def test_snapshot_carries_screen_says_and_queue(client):
    info = await register(client)
    sid = info["session_id"]
    seen = []
    client.bus.subscribe(seen.append)
    await client.post(
        "/v1/bridge/screen",
        json={"session_id": sid, "markdown": "# plan", "title": "Plan", "mode": "show"},
    )
    await client.post("/v1/bridge/say", json={"session_id": sid, "text": "hi there"})
    client.registry.dispatch("do it", client.registry.mint_turn_id())
    state = await (await client.post("/v1/control/state.get", json={})).json()
    s = state["sessions"][0]
    assert s["screen_markdown"] == "# plan"
    assert s["say_tail"][-1]["text"] == "hi there"
    assert s["queued"] == 1
    # screen.updated rides the full content so UIs never refetch.
    assert any(
        e.type == "screen.updated" and e.payload.get("markdown") == "# plan"
        for e in seen
    )


async def test_ui_served_without_auth_ws_token_via_query():
    bus = EventBus()
    registry = Registry(emit=bus.emit)
    server = BridgeServer(registry, bus, token="sekrit", listen_slice_s=0.2)
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    try:
        for path in ("/", "/ui"):
            resp = await c.get(path)
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            assert "voco" in await resp.text()
        # Browsers cannot set WS headers: ?token= must authenticate.
        ws = await c.ws_connect("/v1/events?token=sekrit")
        snap = await ws.receive_json()
        assert snap["type"] == "snapshot"
        await ws.close()
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await c.ws_connect("/v1/events")  # no token still 401s
    finally:
        await c.close()


async def test_register_instance_separates_same_cwd_agents(client):
    """HTTP register must carry the instance discriminator end-to-end."""
    a = await (
        await client.post("/v1/bridge/register", json={**IDENT, "instance": "%5"})
    ).json()
    b = await (
        await client.post("/v1/bridge/register", json={**IDENT, "instance": "%9"})
    ).json()
    assert a["session_id"] != b["session_id"]
    assert a["call_name"] != b["call_name"]


async def test_snapshot_extra_merges_daemon_state():
    """UI truth source: mic state rides every snapshot (WS + state.get)."""
    bus = EventBus()
    registry = Registry(emit=bus.emit)
    mic = {"duplex": "full_duplex", "attention": "always"}
    server = BridgeServer(
        registry, bus, listen_slice_s=0.2, snapshot_extra=lambda: {"mic": dict(mic)}
    )
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    try:
        ws = await c.ws_connect("/v1/events")
        snap = await ws.receive_json()
        assert snap["payload"]["mic"] == mic
        # Live state changes show up in the NEXT snapshot (reconnect/state.get).
        mic["duplex"] = "half_duplex"
        await ws.send_json({"id": "1", "cmd": "state.get", "payload": {}})
        reply = await ws.receive_json()
        assert reply["payload"]["mic"]["duplex"] == "half_duplex"
        await ws.close()
        resp = await c.post("/v1/control/state.get", json={})
        assert (await resp.json())["mic"]["duplex"] == "half_duplex"
    finally:
        await c.close()


async def test_ws_snapshot_then_events(client):
    await register(client)
    ws = await client.ws_connect("/v1/events")
    snapshot = await ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert len(snapshot["payload"]["sessions"]) == 1
    # A live event flows after the snapshot.
    client.bus.emit("mic.state", {"mode": "full_duplex"})
    event = await ws.receive_json()
    assert event["type"] == "mic.state"
    assert event["seq"] > snapshot["seq"]
    # Command envelope round-trip.
    await ws.send_json({"id": "1", "cmd": "state.get", "payload": {}})
    reply = await ws.receive_json()
    assert reply["id"] == "1" and reply["ok"] is True
    await ws.close()
