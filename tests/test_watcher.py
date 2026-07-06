"""Pane watcher: capture set, edge detection, confirm-before-speak."""

from __future__ import annotations

from voco.core.registry import Registry
from voco.watcher import PaneWatcher

WAITING = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
WORKING = "✻ Churning… (12s · esc to interrupt)\n"


class FakeTmux:
    def __init__(self) -> None:
        self.by_target: dict[str, str] = {}
        self.captured: list[str] = []

    def capture_pane(self, target: str, host: str | None = None) -> str:
        self.captured.append(target)
        if target not in self.by_target:
            raise RuntimeError("no such pane")
        return self.by_target[target]


def make_world():
    events = []
    r = Registry(emit=lambda t, p: events.append((t, p)))
    tmux_s = r.register(
        {"host": "m", "cwd": "/a", "harness": "claude", "tmux_pane": "%1"},
        ["say", "listen"],
    )
    plain = r.register({"host": "m", "cwd": "/b", "harness": "codex"}, ["say"])
    parked = r.register(
        {"host": "m", "cwd": "/c", "harness": "claude", "tmux_pane": "%2"},
        ["say", "listen"],
    )
    parked.parked = True
    return r, events, tmux_s, plain, parked


async def test_watches_only_injectable_unparked_sessions():
    r, events, s, _, _ = make_world()
    tmux = FakeTmux()
    tmux.by_target["%1"] = WORKING
    w = PaneWatcher(r, tmux)
    await w.poll_once()
    assert tmux.captured == ["%1"]  # no plain session, no parked pane
    assert r.get(s.session_id).pane_hint == "working"
    assert [t for t, _ in events if t == "pane.hint"] == ["pane.hint"]
    # Unchanged hint on the next poll: no duplicate event.
    await w.poll_once()
    assert [t for t, _ in events if t == "pane.hint"] == ["pane.hint"]


async def test_waiting_speaks_once_after_two_sightings():
    r, _, s, _, _ = make_world()
    tmux = FakeTmux()
    tmux.by_target["%1"] = WAITING
    spoke = []
    w = PaneWatcher(r, tmux, on_waiting=lambda sess: spoke.append(sess.call_name))
    await w.poll_once()
    assert spoke == []  # one sighting could be a scroll artifact
    await w.poll_once()
    assert spoke == [s.call_name]  # confirmed -> announced
    await w.poll_once()
    assert spoke == [s.call_name]  # same episode: never re-announced
    # Episode ends (user answered, agent works), then a NEW prompt appears.
    tmux.by_target["%1"] = WORKING
    await w.poll_once()
    tmux.by_target["%1"] = WAITING
    await w.poll_once()
    await w.poll_once()
    assert spoke == [s.call_name, s.call_name]


async def test_capture_failure_is_a_none_hint_not_an_error():
    r, events, s, _, _ = make_world()
    tmux = FakeTmux()  # %1 missing -> capture raises
    w = PaneWatcher(r, tmux)
    r.get(s.session_id).pane_hint = "working"  # pretend we knew something
    await w.poll_once()  # must not raise
    assert r.get(s.session_id).pane_hint is None
    assert not any(t == "daemon.error" for t, _ in events)


def test_grounding_carries_terminal_hint():
    from voco.core.first_mate import build_grounding

    r, _, s, _, _ = make_world()
    r.set_pane_hint(s.session_id, "waiting")
    g = build_grounding(r, "full_duplex", now=0.0)
    by_name = {x["name"]: x for x in g["sessions"]}
    assert by_name[s.call_name]["terminal"] == "waiting"


async def test_pty_sessions_watched_via_injected_capture():
    """W4: daemon-owned ptys have no tmux pane — the injected capture
    keeps them watched (SPEC-WORKBENCH §5: backend-agnostic watcher)."""
    r, _events, _tmux_s, _plain, _parked = make_world()
    pty_s = r.register(
        {"host": "m", "cwd": "/d", "harness": "claude", "instance": "pty-1"},
        ["say", "listen"],
    )
    tmux = FakeTmux()
    tmux.by_target["%1"] = WORKING

    def pty_capture(s):
        return WAITING if s.session_id == pty_s.session_id else None

    w = PaneWatcher(r, tmux, pty_capture=pty_capture)
    await w.poll_once()
    await w.poll_once()
    assert pty_s.pane_hint == "waiting"
    assert "%1" in tmux.captured  # tmux sessions still watched via tmux


async def test_strip_ansi_lets_classify_see_prompts():
    from voco.adapters.ptyterm import strip_ansi
    from voco.core.pane_state import classify

    raw = (
        "\x1b[2J\x1b[1;1H\x1b[1mDo you want to proceed?\x1b[0m\r\n"
        "\x1b[36m❯ 1. Yes\x1b[0m\r\n  2. No\r\n"
    )
    assert classify(strip_ansi(raw)) == "waiting"
