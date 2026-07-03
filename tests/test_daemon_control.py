"""Daemon control surface (detach/peek) + graceful shutdown."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from voco.adapters.tmux import RunResult, TmuxManager
from voco.daemon import Daemon


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdout = "line one\nline two\n"

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        return RunResult(0, self.stdout, "")


@pytest.fixture
def daemon() -> tuple[Daemon, FakeRunner]:
    d = Daemon({}, no_audio=True)
    runner = FakeRunner()
    d._tmux_mgr = TmuxManager(runner)
    return d, runner


async def test_detach_removes_session_and_clears_active(daemon):
    d, _ = daemon
    s = d.registry.register({"host": "mac", "cwd": "/a", "harness": "claude"}, ["say"])
    result = await d._control("session.detach", {"name": s.call_name})
    assert result == {"detached": s.call_name}
    assert d.registry.get(s.session_id) is None
    assert d.registry.active is None  # no auto-election


async def test_detach_unknown_name_raises(daemon):
    d, _ = daemon
    with pytest.raises(ValueError, match="no session named"):
        await d._control("session.detach", {"name": "Nobody"})


async def test_peek_by_call_name_uses_session_pane(daemon):
    d, runner = daemon
    s = d.registry.register(
        {"host": "mac", "cwd": "/a", "harness": "claude", "tmux_pane": "%7"},
        ["say", "listen"],
    )
    result = await d._control("session.peek", {"name": s.call_name})
    assert result["text"] == "line one\nline two\n"
    assert result["hint"] is None  # ordinary output: no state claim
    assert runner.calls[-1] == ["tmux", "capture-pane", "-p", "-t", "%7"]


async def test_peek_hint_flags_waiting_prompt(daemon):
    d, runner = daemon
    runner.stdout = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
    result = await d._control("session.peek", {"target": "voco-x"})
    assert result["hint"] == "waiting"


async def test_peek_raw_target_and_remote_host(daemon):
    d, runner = daemon
    await d._control("session.peek", {"target": "voco-claude", "host": "ws"})
    assert runner.calls[-1][:3] == ["ssh", "-T", "ws"]
    assert runner.calls[-1][-1] == "voco-claude"


async def test_peek_without_terminal_raises(daemon):
    d, _ = daemon
    s = d.registry.register({"host": "mac", "cwd": "/b", "harness": "codex"}, ["say"])
    with pytest.raises(ValueError, match="no terminal to peek"):
        await d._control("session.peek", {"name": s.call_name})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
async def test_sigterm_shuts_down_cleanly(tmp_path):
    d = Daemon({"state": {"dir": str(tmp_path)}}, no_audio=True)
    task = asyncio.create_task(d.run(port=0))
    await asyncio.sleep(0.2)  # let the server come up + handlers install
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)
