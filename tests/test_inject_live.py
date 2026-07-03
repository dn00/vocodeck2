"""Live tmux integration (SPEC §9.2 + inject) — skipped when tmux is absent.

Spawns a real detached tmux session running `cat`, types into it via
send_text, reads it back via capture_pane, and kills it. This is the test
that was 'pending — no tmux on the Mac' in BUILD.md.
"""

from __future__ import annotations

import shutil
import time

import pytest

from voco.adapters.tmux import TmuxManager

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


def test_spawn_inject_capture_kill_roundtrip():
    mgr = TmuxManager()
    name = mgr.spawn("cat", name="pytest-inject")
    try:
        assert name in mgr.list()
        mgr.send_text(name, "hello from voco inject")
        deadline = time.time() + 5
        seen = ""
        while time.time() < deadline:
            seen = mgr.capture_pane(name)
            if "hello from voco inject" in seen:
                break
            time.sleep(0.2)
        assert "hello from voco inject" in seen
        mgr.send_escape(name)  # must not error against a live pane
    finally:
        mgr.kill(name)
    assert name not in mgr.list()


def test_registry_marks_inject_capability():
    from voco.core.registry import Registry

    r = Registry()
    s = r.register(
        {"host": "mac", "cwd": "/x", "harness": "claude", "tmux_pane": "%5"},
        ["say", "listen"],
    )
    assert "inject" in s.capabilities
    assert s.inject_target == "%5"
    plain = r.register({"host": "mac", "cwd": "/y", "harness": "codex"}, ["say"])
    assert "inject" not in plain.capabilities
