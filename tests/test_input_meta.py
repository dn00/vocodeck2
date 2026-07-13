"""Input provenance + backlog staleness (live-test bugs: typed input was
indistinguishable from speech; a slow agent got an undifferentiated wall
of stale transcripts)."""

from __future__ import annotations

import pytest

from voco.core.limits import MAX_INPUT_BYTES, MAX_QUEUED_INPUTS
from voco.core.registry import QueuedInput, Registry
from voco_cli.main import format_transcript


def ident() -> dict:
    return {"host": "mac", "user": "dn", "cwd": "/repo/a", "harness": "claude"}


def test_queued_inputs_carry_origin_and_age():
    clock = {"t": 1000.0}
    r = Registry(now=lambda: clock["t"])
    s = r.register(ident(), ["say", "listen"])
    # This is a backlog-age test, so model a live agent working between
    # listener polls. An idle session with no listener for 150s is correctly
    # disconnected by DF-10 and must reject new input.
    s.outstanding_turn_id = "t-working"
    r.dispatch("spoken early", r.mint_turn_id())
    clock["t"] += 150
    r.dispatch("typed later", r.mint_turn_id(), origin="typed")
    clock["t"] += 30
    payload = r.on_listen_start(s.session_id)
    assert payload is not None
    # Newest queued item is the current instruction; older items remain
    # chronological backlog so the formatter outputs early -> later.
    assert payload["text"] == "typed later"
    assert payload["origin"] == "typed"
    assert payload["age_s"] == 30
    (q,) = payload["queued"]
    assert q["origin"] == "voice" and q["age_s"] == 180


def test_queue_events_converge_count_after_drain():
    events = []
    r = Registry(emit=lambda t, p: events.append((t, p)))
    s = r.register(ident(), ["say", "listen"])
    r.dispatch("one", r.mint_turn_id())
    r.dispatch("two", r.mint_turn_id())
    queued = [p for t, p in events if t == "input.queued"]
    assert [p["queued"] for p in queued] == [1, 2]
    r.on_listen_start(s.session_id)
    assert events[-1] == (
        "input.drained",
        {"session_id": s.session_id, "queued": 0},
    )


def test_queue_limit_rejects_new_input_without_side_effects():
    events: list[tuple[str, dict]] = []
    r = Registry(emit=lambda topic, payload: events.append((topic, payload)))
    s = r.register(ident(), ["say", "listen"])

    for index in range(MAX_QUEUED_INPUTS):
        r.dispatch(f"command {index}", r.mint_turn_id())

    queue_before = list(s.queued)
    history_before = list(s.input_log)
    events_before = list(events)
    with pytest.raises(ValueError, match="input queue is full"):
        r.dispatch("rejected", r.mint_turn_id())

    assert s.queued == queue_before
    assert list(s.input_log) == history_before
    assert events == events_before


def test_live_delivery_succeeds_when_old_queue_is_at_cap():
    r = Registry()
    s = r.register(ident(), ["say", "listen"])
    s.queued = [
        QueuedInput(ts=0, turn_id=f"t-{index}", text=str(index))
        for index in range(MAX_QUEUED_INPUTS)
    ]
    delivered: list[dict] = []
    r.try_deliver = lambda sid, payload: (delivered.append(payload), True)[1]
    s.parked = True

    assert r.dispatch("live", r.mint_turn_id()) == "live"
    assert delivered[0]["text"] == "live"
    assert s.queued == []


def test_input_limit_uses_utf8_bytes():
    r = Registry()
    s = r.register(ident(), ["say", "listen"])
    exact = "é" * (MAX_INPUT_BYTES // 2)
    assert r.dispatch(exact, r.mint_turn_id()) == "queued_idle"
    assert s.queued[-1].text == exact

    with pytest.raises(ValueError, match="input exceeds maximum size"):
        r.dispatch(exact + "é", r.mint_turn_id())
    assert len(s.queued) == 1


def test_live_dispatch_is_fresh_and_marks_origin():
    r = Registry()
    s = r.register(ident(), ["say", "listen"])
    delivered: list[dict] = []
    r.try_deliver = lambda sid, payload: (delivered.append(payload), True)[1]
    r.on_listen_start(s.session_id)  # parks
    r.dispatch("do it", r.mint_turn_id(), origin="typed")
    assert delivered[0]["origin"] == "typed" and delivered[0]["age_s"] == 0


def test_format_transcript_marks_stale_and_typed():
    result = {
        "status": "transcript",
        "text": "current instruction",
        "origin": "voice",
        "age_s": 0,
        "queued": [
            {"text": "old spoken", "origin": "voice", "age_s": 200},
            {"text": "fresh typed", "origin": "typed", "age_s": 3},
        ],
    }
    out = format_transcript(result)
    lines = out.splitlines()
    assert lines[0] == "[queued while working, 3m ago] old spoken"
    assert lines[1] == "[queued while working, typed] fresh typed"
    assert lines[-1] == "current instruction"  # fresh voice line: unmarked


def test_format_transcript_marks_stale_main_line():
    result = {"status": "transcript", "text": "do the thing", "age_s": 3700}
    assert format_transcript(result) == "[1h ago] do the thing"


def test_format_transcript_tolerates_legacy_payloads():
    # A daemon predating origin/age: plain rendering, no crash.
    result = {"status": "transcript", "text": "hello", "queued": [{"text": "q1"}]}
    out = format_transcript(result)
    assert out == "[queued while working] q1\nhello"


class SequencedClient:
    """listen() pops a scripted result per call (duck-typed for the CLI)."""

    def __init__(self, results: list[dict]) -> None:
        self._results = iter(results)

    def listen(self) -> dict:
        return next(self._results)


def test_listen_stream_prints_each_transcript_until_detach(capsys):
    from voco_cli.main import listen_stream

    rc = listen_stream(
        SequencedClient(
            [
                {"status": "transcript", "text": "one", "queued": []},
                {"status": "transcript", "text": "two", "queued": []},
                {"status": "detach", "reason": "shutdown"},
            ]
        )
    )
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["one", "two"]
    assert "shutting down" in out[-1]


def test_listen_stream_stops_when_superseded(capsys):
    from voco_cli.main import listen_stream

    rc = listen_stream(SequencedClient([{"status": "superseded"}]))
    assert rc == 0
    assert "another listener took over" in capsys.readouterr().out


def test_terminal_messages_distinguish_endings():
    """A user detach must never read as a crash (live-test bug)."""
    from voco_cli.main import (
        MSG_DETACHED,
        MSG_SHUTDOWN,
        MSG_SUPERSEDED,
        SOFT_FAIL,
        terminal_message,
    )

    assert terminal_message({"status": "detach", "reason": "detached"}) == (
        MSG_DETACHED
    )
    assert terminal_message({"status": "detach", "reason": "shutdown"}) == (
        MSG_SHUTDOWN
    )
    assert terminal_message({"status": "detach"}) == MSG_SHUTDOWN  # legacy
    assert terminal_message({"status": "superseded"}) == MSG_SUPERSEDED
    assert terminal_message({"status": "unavailable"}) == SOFT_FAIL
    assert terminal_message({"status": "transcript"}) is None
    assert "do not restart" in MSG_DETACHED  # agents read these verbatim


def test_listen_stream_exits_softly_when_daemon_gone(capsys):
    from voco_cli.main import listen_stream

    rc = listen_stream(SequencedClient([{"status": "unavailable"}]))
    assert rc == 0
    assert "continue without voice" in capsys.readouterr().out


class InitClient:
    """Duck-typed Client for init_reply: canned session, fixed url/token."""

    def __init__(self, token: str | None = None, fail: bool = False) -> None:
        self.base_url = "http://127.0.0.1:7799"
        self.token = token
        self._fail = fail
        self._identity = {
            "cwd": "/repo/firstmate",
            "harness": "claude",
            "instance": "%42",
        }

    def session(self) -> dict:
        if self._fail:
            raise OSError("daemon down")
        return {"session_id": "abc", "call_name": "Petra"}


def test_voice_init_writes_both_scripts_and_full_instructions(tmp_path, monkeypatch):
    import sys

    import voco_mcp.main as mcp_main

    monkeypatch.setattr(mcp_main, "CACHE_DIR", tmp_path)
    out = mcp_main.init_reply(InitClient())
    oneshot = tmp_path / "listen.sh"
    stream = tmp_path / "listen-stream.sh"
    assert "You are Petra" in out
    assert f"bash {oneshot}" in out
    assert f"bash {stream}" in out
    # One-shot: exit-per-transcript wakes exit-notification harnesses.
    body = oneshot.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "export VOCO_URL=http://127.0.0.1:7799" in body
    assert "export VOCO_INSTANCE=%42" in body
    assert "export VOCO_HARNESS=claude" in body
    assert "cd /repo/firstmate" in body
    # Pins THIS interpreter: the agent's shell has no `voco` on PATH.
    assert f"exec {sys.executable} -m voco_cli.main listen\n" in body
    assert "--stream" not in body
    assert "VOCO_TOKEN" not in body
    assert (oneshot.stat().st_mode & 0o777) == 0o700
    # Streaming: for harnesses that can monitor live background output.
    sbody = stream.read_text()
    assert f"exec {sys.executable} -m voco_cli.main listen --stream\n" in sbody
    assert (stream.stat().st_mode & 0o777) == 0o700
    # Self-contained integration instructions (live-test ask): the reply
    # alone must teach the loop and the stop conditions.
    assert "re-run the same command" in out
    assert "Do NOT call voice_listen" in out
    assert "STOP re-running" in out


def test_voice_init_keeps_token_in_the_script_not_the_reply(tmp_path, monkeypatch):
    import voco_mcp.main as mcp_main

    monkeypatch.setattr(mcp_main, "CACHE_DIR", tmp_path)
    out = mcp_main.init_reply(InitClient(token="sekrit"))
    assert "sekrit" not in out  # never in the transcript
    assert "export VOCO_TOKEN=sekrit" in (tmp_path / "listen.sh").read_text()


def test_voice_init_fails_soft_when_daemon_unreachable(tmp_path, monkeypatch):
    import voco_mcp.main as mcp_main
    from voco_cli.main import SOFT_FAIL

    monkeypatch.setattr(mcp_main, "CACHE_DIR", tmp_path)
    assert mcp_main.init_reply(InitClient(fail=True)) == SOFT_FAIL
    assert not (tmp_path / "listen.sh").exists()  # nothing written on failure


def test_derive_identity_honors_baked_harness(monkeypatch):
    from voco_cli.main import derive_identity

    monkeypatch.setenv("VOCO_HARNESS", "claude")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert derive_identity()["harness"] == "claude"
