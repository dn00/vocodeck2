"""TTS floor supervision (BUILD-PROD P3): restart-on-crash with capped
backoff, clean stop, honest spawn failure."""

from __future__ import annotations

import asyncio
import sys

from voco.adapters.floor_supervisor import (
    FloorSupervisor,
    floor_argv,
    should_manage,
)


def test_floor_argv_carries_port():
    argv = floor_argv(8881)
    assert argv[-2:] == ["--port", "8881"]


def test_should_manage_decision_table():
    # default config → managed on the floor's own port
    assert should_manage({}) == 8880
    assert should_manage({"base_url": "http://127.0.0.1:8880/v1"}) == 8880
    # a custom local engine on another port: NOT managed unless asked
    assert should_manage({"base_url": "http://127.0.0.1:9000/v1"}) is None
    assert (
        should_manage({"base_url": "http://127.0.0.1:9000/v1", "manage_floor": True})
        == 9000
    )
    # explicit off always wins
    assert should_manage({"manage_floor": False}) is None
    # never supervise something we can't own (remote engine)
    assert should_manage({"base_url": "http://gpu-box:8880/v1"}) is None
    assert (
        should_manage({"base_url": "http://gpu-box:8880/v1", "manage_floor": True})
        is None
    )


async def test_stop_terminates_long_running_child():
    events: list[tuple[str, dict]] = []
    sup = FloorSupervisor(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        emit=lambda t, p: events.append((t, p)),
    )
    await sup.start()
    await asyncio.sleep(0.3)  # child is up
    assert sup._proc is not None and sup._proc.returncode is None
    await sup.stop()
    assert sup._proc.returncode is not None
    assert sup.restarts == 0
    assert not events  # a clean managed stop is not an error


async def test_crashing_child_restarts_with_backoff_and_emits():
    events: list[tuple[str, dict]] = []
    sup = FloorSupervisor(
        [sys.executable, "-c", "raise SystemExit(3)"],
        emit=lambda t, p: events.append((t, p)),
        backoff_start=0.05,
        backoff_cap=0.1,
    )
    await sup.start()
    await asyncio.sleep(1.0)
    await sup.stop()
    assert sup.restarts >= 2  # it kept trying
    assert events and all(t == "daemon.error" for t, _ in events)
    assert "rc=3" in events[0][1]["error"]


async def test_unspawnable_argv_retries_then_gives_up():
    events: list[tuple[str, dict]] = []
    sup = FloorSupervisor(
        ["/nonexistent/binary-xyz"],
        emit=lambda t, p: events.append((t, p)),
        backoff_start=0.02,
        backoff_cap=0.03,
    )
    await sup.start()
    await asyncio.sleep(0.6)
    await sup.stop()
    # transient spawn errors retry (EMFILE could heal); a persistently
    # broken argv gets 5 attempts and a terminal give-up
    spawn_msgs = [e for _, e in events if "failed to spawn" in e["error"]]
    assert len(spawn_msgs) == 5
    assert any("giving up" in e["error"] for _, e in events)
    assert sup.restarts == 0
