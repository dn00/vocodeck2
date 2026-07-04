"""Input provenance + backlog staleness (live-test bugs: typed input was
indistinguishable from speech; a slow agent got an undifferentiated wall
of stale transcripts)."""

from __future__ import annotations

from voco.core.registry import Registry
from voco_cli.main import format_transcript


def ident() -> dict:
    return {"host": "mac", "user": "dn", "cwd": "/repo/a", "harness": "claude"}


def test_queued_inputs_carry_origin_and_age():
    clock = {"t": 1000.0}
    r = Registry(now=lambda: clock["t"])
    s = r.register(ident(), ["say", "listen"])
    r.dispatch("spoken early", r.mint_turn_id())
    clock["t"] += 150
    r.dispatch("typed later", r.mint_turn_id(), origin="typed")
    clock["t"] += 30
    payload = r.on_listen_start(s.session_id)
    assert payload is not None
    # First queued item becomes the transcript, with its true age.
    assert payload["text"] == "spoken early"
    assert payload["origin"] == "voice"
    assert payload["age_s"] == 180
    (q,) = payload["queued"]
    assert q["origin"] == "typed" and q["age_s"] == 30


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

    def session(self) -> dict:
        if self._fail:
            raise OSError("daemon down")
        return {"session_id": "abc", "call_name": "Petra"}


def test_voice_init_writes_script_and_returns_bash_command(tmp_path, monkeypatch):
    import sys

    import voco_mcp.main as mcp_main

    monkeypatch.setattr(mcp_main, "CACHE_DIR", tmp_path)
    out = mcp_main.init_reply(InitClient())
    script = tmp_path / "listen.sh"
    assert "You are Petra" in out
    assert f"bash {script}" in out  # the exact, backgroundable command
    body = script.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "export VOCO_URL=http://127.0.0.1:7799" in body
    # Pins THIS interpreter: the agent's shell has no `voco` on PATH.
    # ONE-SHOT (no --stream): exit-per-transcript is what wakes the agent.
    assert f"exec {sys.executable} -m voco_cli.main listen\n" in body
    assert "--stream" not in body
    assert "VOCO_TOKEN" not in body
    assert (script.stat().st_mode & 0o777) == 0o700
    # The reply must teach the re-arm loop.
    assert "run the same command again" in out


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
