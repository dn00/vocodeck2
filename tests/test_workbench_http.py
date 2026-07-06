"""Workbench HTTP surface: §8.5 browser defense, page push/read, snapshot."""

from __future__ import annotations

import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

from voco.core.events import EventBus
from voco.core.registry import Registry
from voco.core.workspace import WorkspaceStore
from voco.server.http import BridgeServer
from voco.server.workbench import handle_workbench_command

HOST = socket.gethostname().split(".")[0]


@pytest.fixture
async def client(tmp_path):
    bus = EventBus()
    registry = Registry(emit=bus.emit)
    store = WorkspaceStore(emit=bus.emit)

    async def control(cmd, payload):
        try:
            return handle_workbench_command(store, cmd, payload, data_dir=tmp_path)
        except KeyError:
            raise ValueError(f"unknown command {cmd!r}") from None

    server = BridgeServer(
        registry,
        bus,
        listen_slice_s=0.5,
        workspaces=store,
        allowed_origins=["https://proxy.example"],
        on_control=control,
    )
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    c.server_obj = server
    c.store = store
    c.repo = tmp_path
    yield c
    await c.close()


def ident(repo) -> dict:
    return {
        "host": HOST,  # matches the daemon's — a local_fs session
        "user": "d",
        "cwd": str(repo),
        "repo": repo.name,
        "branch": "main",
        "worktree": str(repo),
        "harness": "claude",
    }


async def register(client) -> str:
    resp = await client.post("/v1/bridge/register", json=ident(client.repo))
    assert resp.status == 200
    return (await resp.json())["session_id"]


# ---- §8.5: origin discipline + workbench token ------------------------------


async def test_foreign_origin_rejected_on_mutations_and_ws(client):
    evil = {"Origin": "https://evil.example"}
    resp = await client.post(
        "/v1/control/say_as_user", json={"text": "rm -rf"}, headers=evil
    )
    assert resp.status == 403
    resp = await client.post(
        "/v1/bridge/register", json=ident(client.repo), headers=evil
    )
    assert resp.status == 403
    resp = await client.get("/v1/events", headers={**evil, "Connection": "upgrade"})
    assert resp.status == 403


async def test_loopback_origin_any_port_needs_wb_token(client):
    origin = {"Origin": "http://localhost:9999"}
    resp = await client.post("/v1/control/state.get", json={}, headers=origin)
    assert resp.status == 403  # loopback origin passes, but no wb token
    wb = client.server_obj.wb_token
    resp = await client.post(
        "/v1/control/state.get", json={}, headers={**origin, "x-voco-wb": wb}
    )
    assert resp.status == 200


async def test_allowed_origins_config_passes_with_token(client):
    headers = {
        "Origin": "https://proxy.example",
        "x-voco-wb": client.server_obj.wb_token,
    }
    resp = await client.post("/v1/control/state.get", json={}, headers=headers)
    assert resp.status == 200


async def test_no_origin_curl_passes_as_before(client):
    resp = await client.post("/v1/control/state.get", json={})
    assert resp.status == 200


async def test_browser_ws_without_wb_rejected_at_upgrade(client):
    # BLOCKER 1: a browser-origin WS with no wb cannot even READ the event
    # stream (the snapshot leaks cwds/screens/findings). Upgrade → 403.
    resp = await client.get("/v1/events", headers={"Origin": "http://127.0.0.1:9999"})
    assert resp.status == 403


async def test_no_origin_ws_reads_events_and_runs_commands(client):
    # CLI tools (no Origin) keep full access under the bearer policy.
    async with client.ws_connect("/v1/events") as ws:
        snap = await ws.receive_json()
        assert snap["type"] == "snapshot"
        await ws.send_json({"id": "1", "cmd": "state.get", "payload": {}})
        reply = await ws.receive_json()
        assert reply["ok"] is True


async def test_browser_ws_with_wb_reads_and_commands(client):
    wb = client.server_obj.wb_token
    async with client.ws_connect(
        f"/v1/events?wb={wb}", headers={"Origin": "http://127.0.0.1:7777"}
    ) as ws:
        await ws.receive_json()  # snapshot
        await ws.send_json({"id": "2", "cmd": "state.get", "payload": {}})
        reply = await ws.receive_json()
        assert reply["ok"] is True


# ---- served pages ------------------------------------------------------------


async def test_shell_and_debug_inject_token_and_csp(client):
    resp = await client.get("/")
    body = await resp.text()
    assert resp.status == 200
    assert client.server_obj.wb_token in body
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "nonce-" in csp
    resp = await client.get("/debug")
    assert client.server_obj.wb_token in await resp.text()


# ---- screen verb -> pinned page; doc push; content reads ----------------------


async def test_screen_verb_becomes_pinned_page_and_snapshot_lists_it(client):
    sid = await register(client)
    resp = await client.post(
        "/v1/bridge/screen",
        json={"session_id": sid, "markdown": "# plan", "title": "Plan"},
    )
    assert (await resp.json())["ok"] is True
    snap = client.server_obj._snapshot()
    (ws_meta,) = snap["workspaces"]
    (page,) = ws_meta["pages"]
    assert page["type"] == "screen" and page["pinned"] is True
    # content served on demand
    resp = await client.get(f"/v1/page/{page['page_id']}")
    body = await resp.json()
    assert body["content"]["markdown"] == "# plan"


async def test_doc_push_path_confined_and_read_fresh(client):
    sid = await register(client)
    doc = client.repo / "DESIGN.md"
    doc.write_text("v1")
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "doc", "path": str(doc)},
    )
    page_id = (await resp.json())["page_id"]

    doc.write_text("v2 fresh")  # read-fresh-per-request
    resp = await client.get(f"/v1/page/{page_id}")
    assert (await resp.json())["content"]["markdown"] == "v2 fresh"

    # confinement: outside the workspace root -> rejected at push
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "doc", "path": "/etc/hostname"},
    )
    assert resp.status == 404


async def test_symlink_escape_fails_on_read(client):
    sid = await register(client)
    inside = client.repo / "linked.md"
    inside.write_text("ok")
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "doc", "path": str(inside)},
    )
    page_id = (await resp.json())["page_id"]
    # swap the file for an escaping symlink AFTER the push (TOCTOU)
    inside.unlink()
    inside.symlink_to("/etc/hostname")
    resp = await client.get(f"/v1/page/{page_id}")
    assert resp.status == 404


async def test_diff_push_content_parses_and_serves(client):
    sid = await register(client)
    patch = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
        "@@ -1,2 +1,2 @@\n import os\n-x=1\n+x=2\n"
    )
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "diff", "content": patch, "name": "wip"},
    )
    body = await resp.json()
    assert body["ok"] is True
    resp = await client.get(f"/v1/page/{body['page_id']}")
    files = (await resp.json())["content"]["files"]
    assert files[0]["path"] == "f.py"
    kinds = [r["kind"] for r in files[0]["hunks"][0]["rows"]]
    assert kinds == ["context", "del", "add"]


async def test_finding_round_trip_add_then_agent_status(client):
    sid = await register(client)
    patch = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "diff", "content": patch, "name": "d"},
    )
    page_id = (await resp.json())["page_id"]
    ws_key = client.store.resolve(
        {"host": HOST, "worktree": str(client.repo), "cwd": str(client.repo)}
    ).key
    # human adds a finding via control (workbench origin + wb)
    wb = client.server_obj.wb_token
    add = await client.post(
        "/v1/control/finding.add",
        json={
            "workspace": ws_key,
            "page_id": page_id,
            "anchor": {"file": "f", "side": "new", "startLine": 1, "endLine": 1},
            "text": "why b?",
            "kind": "question",
        },
        headers={"Origin": "http://127.0.0.1:7777", "x-voco-wb": wb},
    )
    fid = (await add.json())["finding"]["finding_id"]
    # agent lists pending
    resp = await client.get(f"/v1/bridge/findings?session_id={sid}&pending=1")
    findings = (await resp.json())["findings"]
    assert len(findings) == 1 and findings[0]["finding_id"] == fid
    # agent marks addressed
    resp = await client.post(
        "/v1/bridge/finding_status",
        json={
            "session_id": sid,
            "finding_id": fid,
            "status": "addressed",
            "commit": "deadbeef",
        },
    )
    assert (await resp.json())["finding"]["status"] == "addressed"
    # agent cannot withdraw
    resp = await client.post(
        "/v1/bridge/finding_status",
        json={"session_id": sid, "finding_id": fid, "status": "withdrawn"},
    )
    assert resp.status == 400


async def test_finding_status_confined_to_own_workspace(client, tmp_path_factory):
    sid = await register(client)
    # another session in a different repo
    other = tmp_path_factory.mktemp("other")
    oident = {
        **ident(client.repo),
        "cwd": str(other),
        "worktree": str(other),
        "repo": "other",
    }
    resp = await client.post("/v1/bridge/register", json=oident)
    osid = (await resp.json())["session_id"]
    # finding in client.repo's workspace
    patch = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "diff", "content": patch, "name": "d"},
    )
    page_id = (await resp.json())["page_id"]
    ws_key = client.store.resolve(
        {"host": HOST, "worktree": str(client.repo), "cwd": str(client.repo)}
    ).key
    wb = client.server_obj.wb_token
    add = await client.post(
        "/v1/control/finding.add",
        json={
            "workspace": ws_key,
            "page_id": page_id,
            "anchor": {"file": "f"},
            "text": "x",
        },
        headers={"Origin": "http://127.0.0.1:7777", "x-voco-wb": wb},
    )
    fid = (await add.json())["finding"]["finding_id"]
    # the OTHER session cannot touch it
    resp = await client.post(
        "/v1/bridge/finding_status",
        json={"session_id": osid, "finding_id": fid, "status": "addressed"},
    )
    assert resp.status == 404


async def test_remote_session_path_push_soft_rejected(client):
    remote = {**ident(client.repo), "host": "far-away-box"}
    resp = await client.post("/v1/bridge/register", json=remote)
    sid = (await resp.json())["session_id"]
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "doc", "path": str(client.repo / "x.md")},
    )
    assert resp.status == 400
    assert "push content instead" in await resp.text()
    # content push from remote works
    resp = await client.post(
        "/v1/bridge/page",
        json={"session_id": sid, "type": "doc", "name": "notes", "content": "hi"},
    )
    assert (await resp.json())["ok"] is True
