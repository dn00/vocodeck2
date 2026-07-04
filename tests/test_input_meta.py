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
