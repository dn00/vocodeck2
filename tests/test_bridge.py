"""Bridge integration tests (SPEC §8.1) — real aiohttp server, ephemeral port."""

from __future__ import annotations

import asyncio

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
