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


async def test_child_output_pumps_through_line_log():
    """P4: floor stdout AND stderr land in the structured log, line by
    line, via the injected collector (never test through logging
    globals — the voco logger tree has propagate off)."""
    lines: list[str] = []
    child = (
        "import sys;"
        "print('floor says hi');"
        "print('floor gripes', file=sys.stderr);"
        "sys.stdout.flush(); sys.stderr.flush()"
    )
    sup = FloorSupervisor(
        [sys.executable, "-c", child],
        emit=lambda t, p: None,  # exit-code events are P3's, not under test
        line_log=lines.append,
    )
    await sup.start()
    for _ in range(50):  # child prints and exits; pump reaps on EOF
        if len(lines) >= 2:
            break
        await asyncio.sleep(0.02)
    await sup.stop()
    assert "floor says hi" in lines
    assert "floor gripes" in lines  # stderr merged into the same stream


async def test_pump_survives_oversized_lines():
    lines: list[str] = []
    child = "print('x' * (2 << 20)); print('after the flood')"
    sup = FloorSupervisor(
        [sys.executable, "-c", child],
        emit=lambda t, p: None,
        line_log=lines.append,
    )
    await sup.start()
    for _ in range(100):
        if any("after the flood" in ln for ln in lines):
            break
        await asyncio.sleep(0.05)
    await sup.stop()
    # the monster line was truncated, not fatal — and the pump kept going
    assert any("[line truncated]" in ln for ln in lines)
    assert any("after the flood" in ln for ln in lines)


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


async def test_stop_racing_a_fresh_start_is_clean():
    """xai P4 round: stop() cancels _run — a stop landing right after
    start(), possibly mid-spawn or mid-flood, must neither hang nor
    leave the pump task alive."""
    lines: list[str] = []
    child = "import sys\nwhile True: print('flood' * 100); sys.stdout.flush()"
    sup = FloorSupervisor(
        [sys.executable, "-c", child],
        emit=lambda t, p: None,
        line_log=lines.append,
    )
    await sup.start()
    await sup.stop()  # no settle sleep: race the spawn on purpose
    assert sup._pump_task is None  # reaped, not orphaned
    if sup._proc is not None:
        assert sup._proc.returncode is not None  # child actually dead
