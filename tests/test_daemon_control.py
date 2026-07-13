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
    assert runner.calls[-1][:4] == ["ssh", "-T", "--", "ws"]
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


async def test_background_task_failures_are_observed(daemon):
    d, _ = daemon
    events = []
    d.bus.subscribe(lambda env: events.append((env.type, env.payload)))

    async def fail() -> None:
        raise RuntimeError("boom")

    task = d._spawn_background(fail(), name="test-failure")
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not d._background_tasks
    assert any(
        event_type == "daemon.error"
        and "background task test-failure failed" in payload["error"]
        for event_type, payload in events
    )


# ---- attention refusal is visible to callers (P12 review BLOCKER) --------------


def _wake_unavailable_daemon(tmp_path):
    """A Daemon with a REAL VoiceLoop whose wake detector can never arm
    (wake_loader=None, no wake_model) — the honest 'wake refused' state.
    The real loop, not a fake: the refusal contract lives in VoiceLoop and
    the control surface must relay it, not echo the request back."""
    from fakes import FakeMic, FakePlayer, FakeStt, FakeTts, ScriptedVad
    from voco.voice_loop import VoiceLoop, VoiceLoopDeps

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[audio]\nattention = "always"\n', encoding="utf-8")
    cfg = {
        "audio": {
            "duplex": "full_duplex",
            "attention": "always",
            "phrase_bank_dir": str(tmp_path / "bank"),
        },
        "stt": {"provider": "fake"},
        "tts": {"base_url": "http://none", "model": "x", "voice": "test"},
        "state": {"dir": str(tmp_path / "state")},
    }
    d = Daemon(cfg, no_audio=True, config_path=cfg_path)
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt(""),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
        wake_loader=None,  # the knob: wake can never arm
    )
    d.voice = VoiceLoop(cfg, d.bus, host=d, deps=deps)
    events: list = []
    d.bus.subscribe(lambda env: events.append((env.type, env.payload)))
    return d, events


async def test_mic_set_wake_unavailable_reports_actual_state(tmp_path):
    d, events = _wake_unavailable_daemon(tmp_path)
    result = await d._control("mic.set", {"attention": "wake"})
    assert result["attention"] == "always"  # actual unchanged mode, not the echo
    assert result["wake_available"] is False
    assert "refused" in result
    mic = [p for t, p in events if t == "mic.state"]
    assert mic and mic[-1]["wake_available"] is False


async def test_mic_set_working_attention_has_no_refusal(tmp_path):
    d, _ = _wake_unavailable_daemon(tmp_path)
    result = await d._control("mic.set", {"attention": "ptt_only"})
    assert result["attention"] == "ptt_only"
    assert "refused" not in result


async def test_config_set_wake_unavailable_is_not_applied(tmp_path):
    d, _ = _wake_unavailable_daemon(tmp_path)
    result = await d._control("config.set", {"key": "audio.attention", "value": "wake"})
    assert result["applied"] is False
    assert result["restart_required"] is True
    assert "refused" in result["reason"]
