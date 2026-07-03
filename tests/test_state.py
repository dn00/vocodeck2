"""Durable sessions: registry dump/restore + StateStore + daemon wiring."""

from __future__ import annotations

import asyncio
import json
import stat

from voco.adapters.state_store import StateStore
from voco.core.registry import Registry
from voco.daemon import Daemon


def populated_registry() -> Registry:
    r = Registry()
    a = r.register(
        {"host": "mac", "cwd": "/repo/a", "harness": "claude", "tmux_pane": "%4"},
        ["say", "listen"],
    )
    b = r.register({"host": "mac", "cwd": "/repo/b", "harness": "codex"}, ["say"])
    r.record_say(a.session_id, "tests are green", None)
    r.record_say(b.session_id, "still refactoring", None)
    r.set_screen(a.session_id, "# plan\n- step one", "Plan", "show")
    r.switch(b.call_name)
    r.dispatch("run the linter", r.mint_turn_id(), target=a)  # queues (idle)
    return r


def test_dump_restore_round_trip_preserves_tokens_queues_and_names():
    r1 = populated_registry()
    dump = r1.dump()
    r2 = Registry()
    assert r2.restore(json.loads(json.dumps(dump))) == 2  # JSON-safe
    assert {s.session_id for s in r2.all()} == {s.session_id for s in r1.all()}
    assert r2.call_names() == r1.call_names()
    assert r2.active is not None and r2.active.call_name == r1.active.call_name
    a2 = next(s for s in r2.all() if s.identity["cwd"] == "/repo/a")
    assert "inject" in a2.capabilities and a2.inject_target == "%4"
    assert a2.screen_markdown == "# plan\n- step one"
    assert not a2.parked  # no poll survived the old daemon
    # The queued input survives and delivers on the agent's next listen.
    payload = r2.on_listen_start(a2.session_id)
    assert payload is not None and payload["text"] == "run the linter"
    # Turn counter continues — no id collisions after restore.
    assert r2.mint_turn_id() not in {payload["turn_id"]}


def test_restore_skips_garbage_and_wrong_version():
    r = Registry()
    assert r.restore({"v": 99, "sessions": [{}]}) == 0
    dump = {
        "v": 1,
        "turn_counter": "not-an-int",
        "sessions": [
            {
                "session_id": "ok1",
                "identity": {"host": "m", "cwd": "/x"},
                "call_name": "Iris",
                "capabilities": ["say"],
            },
            {"broken": True},
        ],
    }
    assert r.restore(dump) == 1
    assert r.get("ok1") is not None


def test_state_store_round_trip_perms_and_corruption(tmp_path):
    store = StateStore(tmp_path / "voco")
    store.save({"v": 1, "sessions": []})
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600  # capability tokens live in this file
    data, err = store.load()
    assert err is None and data == {"v": 1, "sessions": []}
    store.path.write_text("{corrupt json", encoding="utf-8")
    data, err = store.load()
    assert data is None and err is not None and "corrupt" in err
    assert store.path.with_suffix(".corrupt").exists()
    # Fresh boot after corruption: (None, None).
    data, err = store.load()
    assert data is None and err is None


def test_daemon_restart_preserves_queued_input(tmp_path):
    cfg = {"state": {"dir": str(tmp_path)}}
    d1 = Daemon(cfg, no_audio=True)
    s = d1.registry.register({"host": "m", "cwd": "/w", "harness": "claude"}, ["say"])
    d1.registry.dispatch("do the thing", d1.registry.mint_turn_id())
    d1._state.save(d1.registry.dump())

    d2 = Daemon(cfg, no_audio=True)
    d2._restore_state()
    restored = d2.registry.get(s.session_id)
    assert restored is not None  # same token: agent's cached session survives
    payload = d2.registry.on_listen_start(s.session_id)
    assert payload is not None and payload["text"] == "do the thing"


async def test_debounced_saver_writes_after_bus_event(tmp_path):
    d = Daemon({"state": {"dir": str(tmp_path)}}, no_audio=True)
    d._wire_state_saver()
    d.registry.register({"host": "m", "cwd": "/w", "harness": "codex"}, ["say"])
    d.bus.emit("digest.updated", {"session_id": "x", "unread": 1})
    assert not d._state.path.exists()  # debounce window still open
    await asyncio.sleep(0.8)
    data, err = d._state.load()
    assert err is None and data is not None and len(data["sessions"]) == 1
