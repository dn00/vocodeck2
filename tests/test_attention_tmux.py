"""Attention gate (SPEC §4.5) and tmux manager (SPEC §9.2, fake runner)."""

from __future__ import annotations

import pytest

from voco.adapters.tmux import RunResult, TmuxManager
from voco.core.attention import AttentionGate, AttentionMode

# ---- attention -------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


def test_always_allows_vad_muted_blocks_everything():
    g = AttentionGate(AttentionMode.ALWAYS)
    assert g.allows_vad() and g.allows_ptt()
    g.set_mode(AttentionMode.MUTED)
    assert not g.allows_vad() and not g.allows_ptt()  # privacy switch


def test_ptt_only_blocks_vad_allows_ptt():
    g = AttentionGate(AttentionMode.PTT_ONLY)
    assert not g.allows_vad() and g.allows_ptt()


def test_wake_window_arms_refreshes_and_expires():
    clock = Clock()
    g = AttentionGate(AttentionMode.WAKE, now=clock.now, wake_window_s=30.0)
    assert not g.allows_vad()  # not yet woken
    g.on_wake_word()
    assert g.allows_vad()
    clock.t = 25.0
    g.on_turn_activity()  # conversation continues: window refreshes
    clock.t = 50.0
    assert g.allows_vad()  # 25 + 30 = 55 > 50
    clock.t = 56.0
    assert not g.allows_vad()  # expired
    g.on_turn_activity()  # activity outside the window must NOT re-arm
    assert not g.allows_vad()
    g.set_mode(AttentionMode.ALWAYS)
    g.set_mode(AttentionMode.WAKE)
    assert not g.allows_vad()  # mode flip cleared any stale window


# ---- tmux -------------------------------------------------------------------


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdout = ""
        self.returncode = 0

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        return RunResult(self.returncode, self.stdout, "boom")


def test_spawn_wires_env_cwd_and_prefix():
    runner = FakeRunner()
    mgr = TmuxManager(runner, voco_url="http://127.0.0.1:7777", sleep=lambda s: None)
    name = mgr.spawn("claude", name="My Repo!", cwd="/repo/a")
    assert name == "voco-my-repo"
    argv = runner.calls[0]
    assert argv[:4] == ["tmux", "new-session", "-d", "-s"]
    assert "voco-my-repo" in argv
    assert "-c" in argv and "/repo/a" in argv
    assert "VOCO_URL=http://127.0.0.1:7777" in argv
    assert argv[-1] == "claude"
    # Startup verification: pin the pane, check it, then unpin.
    flat = [" ".join(c) for c in runner.calls]
    assert any("remain-on-exit on" in c for c in flat)
    assert any("list-panes" in c for c in flat)
    assert any("-u remain-on-exit" in c for c in flat)


def test_remote_spawn_goes_through_ssh():
    runner = FakeRunner()
    mgr = TmuxManager(runner, sleep=lambda s: None)
    mgr.spawn("codex", name="ws", host="workspace")
    # `--` ends ssh option parsing: the host slot can never be an option
    assert runner.calls[0][:5] == ["ssh", "-T", "--", "workspace", "tmux"]
    # Verification calls ride the same ssh transport.
    assert all(c[:4] == ["ssh", "-T", "--", "workspace"] for c in runner.calls)


def test_option_shaped_ssh_host_is_rejected():
    # identity-supplied host: -oProxyCommand=… must never reach ssh
    runner = FakeRunner()
    mgr = TmuxManager(runner, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="invalid ssh host"):
        mgr.spawn("codex", name="ws", host="-oProxyCommand=touch /tmp/pwn")
    assert not runner.calls  # nothing was executed


class ScriptedRunner:
    """Answers by matching a subcommand keyword; records all calls."""

    def __init__(self, script: dict[str, RunResult]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        for key, result in self.script.items():
            if key in " ".join(argv):
                return result
        return RunResult(0, "", "")


def test_spawn_reports_command_that_dies_at_startup():
    """Live-test bug: `voco new` said ok while the session was already
    gone. A dead pane must fail loudly WITH the command's last output."""
    runner = ScriptedRunner(
        {
            "list-panes": RunResult(0, "1 127\n", ""),
            "capture-pane": RunResult(0, "zsh: command not found: claudee\n", ""),
        }
    )
    mgr = TmuxManager(runner, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match=r"status 127.*command not found"):
        mgr.spawn("claudee", name="x")
    assert any("kill-session" in " ".join(c) for c in runner.calls)  # no corpse


def test_spawn_reports_session_that_vanishes_instantly():
    runner = ScriptedRunner({"set-option": RunResult(1, "", "no such session")})
    mgr = TmuxManager(runner, sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="died at spawn"):
        mgr.spawn("claude", name="x")


def test_kill_refuses_non_voco_sessions():
    mgr = TmuxManager(FakeRunner())
    with pytest.raises(ValueError):
        mgr.kill("main")  # a user's own tmux session: hands off


def test_list_filters_to_voco_prefix_and_tolerates_no_server():
    runner = FakeRunner()
    runner.stdout = "main\nvoco-a\nvoco-b\n"
    mgr = TmuxManager(runner)
    assert mgr.list() == ["voco-a", "voco-b"]
    runner.returncode = 1
    assert mgr.list() == []  # no tmux server = no sessions, not an error


def test_failure_surfaces_stderr():
    runner = FakeRunner()
    runner.returncode = 1
    mgr = TmuxManager(runner)
    with pytest.raises(RuntimeError, match="boom"):
        mgr.spawn("claude", name="x")
