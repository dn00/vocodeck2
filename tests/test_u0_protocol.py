"""DESIGN-DECK U0 — the protocol slice under the UI re-architecture:
per-session user-input log (+ session.transcript), speech who/text +
speech.sentence, workspace.open, page.publish. Tests at the command
seam; no UI here."""

from __future__ import annotations

import subprocess

import pytest

from voco.core.arbitration import DuplexMode, PlaybackItem, PlaybackQueue, Source
from voco.core.registry import Registry
from voco.daemon import Daemon
from voco.protocol.messages import make_event, validate_envelope

IDENT = {"host": "box", "cwd": "/repo", "worktree": "/repo", "harness": "claude"}


# ---- user-input log (the transcript's user half) ------------------------------


def test_dispatch_records_input_log_live_and_queued():
    reg = Registry()
    s = reg.register(IDENT, ["say", "listen"])
    # live delivery: parked + a waiter that accepts
    s.parked = True
    reg.try_deliver = lambda sid, payload: True
    assert reg.dispatch("run the tests", "t-1") == "live"
    # queued: agent busy (not parked), input waits
    assert reg.dispatch("and update docs", "t-2", origin="typed") == "queued"
    log = list(s.input_log)
    assert [(line.text, line.origin, line.queued) for line in log] == [
        ("run the tests", "voice", False),
        ("and update docs", "typed", True),
    ]


def test_no_session_dispatch_records_nothing():
    reg = Registry()
    assert reg.dispatch("hello?", "t-1") == "no_session"


def test_input_log_survives_dump_restore():
    reg = Registry()
    s = reg.register(IDENT, ["say"])
    reg.dispatch("first", "t-1")
    reg.dispatch("second", "t-2", origin="typed")
    fresh = Registry()
    assert fresh.restore(reg.dump()) == 1
    restored = fresh.get(s.session_id)
    assert restored is not None
    assert [(line.text, line.queued) for line in restored.input_log] == [
        ("first", True),
        ("second", True),
    ]


def test_transcript_returns_both_halves():
    reg = Registry()
    s = reg.register(IDENT, ["say"])
    reg.dispatch("do the thing", "t-1")
    reg.record_say(s.session_id, "done, pushed", "t-1")
    t = reg.transcript(s.session_id)
    assert t["name"] == s.call_name
    assert [line["text"] for line in t["inputs"]] == ["do the thing"]
    assert [line["text"] for line in t["says"]] == ["done, pushed"]
    with pytest.raises(KeyError):
        reg.transcript("nope")


# ---- speech payloads: who says what -------------------------------------------


class NullPlayer:
    def play(self, item):
        pass

    def stop(self):
        pass


def test_speech_started_and_finished_carry_who_and_text():
    events: list[tuple[str, dict]] = []
    q = PlaybackQueue(NullPlayer(), emit=lambda t, p: events.append((t, p)))
    q.set_duplex(DuplexMode.FULL)
    q.enqueue(
        PlaybackItem(
            Source.AGENT, b"", turn_id="t-1", who="Freya", text="Tests are green."
        )
    )
    q.on_item_finished()
    started = dict(events)["speech.started"]
    finished = dict(events)["speech.finished"]
    for p in (started, finished):
        assert p["who"] == "Freya" and p["text"] == "Tests are green."
    # items without who/text (acks, chimes) keep the lean payload
    events.clear()
    q.enqueue(PlaybackItem(Source.ACK, b""))
    started = dict(events)["speech.started"]
    assert "who" not in started and "text" not in started


# ---- speech.sentence: playback-pull-aligned karaoke feed ------------------------


class StubTts:
    async def stream(self, text, voice=None):
        yield b"pcm:" + text.encode()


async def test_sentence_synth_emits_per_sentence_at_pull():
    from voco.voice_loop import VoiceLoop

    events: list[tuple[str, dict]] = []

    class StubBus:
        def emit(self, type_, payload):
            events.append((type_, payload))

    class StubSelf:
        tts = StubTts()
        _bus = StubBus()

    gen = VoiceLoop._sentence_synth(
        StubSelf(), "First point. Second point!", None, who="Freya", turn_id="t-9"
    )
    chunks = [chunk async for chunk in gen]
    assert chunks == [b"pcm:First point.", b"pcm:Second point!"]
    assert [p["text"] for _, p in events] == ["First point.", "Second point!"]
    assert all(t == "speech.sentence" for t, _ in events)
    assert events[0][1] == {
        "who": "Freya",
        "text": "First point.",
        "index": 0,
        "total": 2,
        "turn_id": "t-9",
    }


async def test_sentence_synth_silent_without_who():
    from voco.voice_loop import VoiceLoop

    events: list[tuple[str, dict]] = []

    class StubBus:
        def emit(self, type_, payload):
            events.append((type_, payload))

    class StubSelf:
        tts = StubTts()
        _bus = StubBus()

    gen = VoiceLoop._sentence_synth(StubSelf(), "Mate line.", None)
    _ = [chunk async for chunk in gen]
    assert events == []  # first-mate speech feeds no karaoke


def test_speech_sentence_is_a_legal_event():
    env = make_event("speech.sentence", {"who": "Freya", "text": "x", "turn_id": None})
    assert env.type == "speech.sentence"


# ---- workspace.open + page.publish (agentless review) --------------------------


@pytest.fixture
def daemon() -> Daemon:
    return Daemon({}, no_audio=True)


def make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return repo


async def test_workspace_open_mints_a_workspace(daemon, tmp_path):
    repo = make_repo(tmp_path)
    result = await daemon._control("workspace.open", {"path": str(repo)})
    assert result["root"] == str(repo.resolve())
    assert result["repo"] == "proj"
    ws = daemon.workspaces.get(result["workspace"])
    assert ws is not None and ws.kind == "workspace"


async def test_workspace_register_mints_no_agent_session(daemon, tmp_path):
    repo = make_repo(tmp_path)
    result = await daemon._control(
        "workspace.register",
        {
            "identity": {
                "host": "remote-box",
                "cwd": str(repo),
                "worktree": str(repo),
                "repo": "proj",
                "branch": "feature",
            }
        },
    )
    assert result["workspace"] == f"remote-box:{repo}"
    assert daemon.registry.all() == []


async def test_workspace_open_rejects_non_checkouts(daemon, tmp_path):
    bare = tmp_path / "plain"
    bare.mkdir()
    with pytest.raises(ValueError, match="not a git checkout"):
        await daemon._control("workspace.open", {"path": str(bare)})
    with pytest.raises(ValueError, match="not a directory"):
        await daemon._control("workspace.open", {"path": str(tmp_path / "ghost")})


PATCH = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"


async def test_page_publish_resolves_and_upserts(daemon, tmp_path):
    repo = make_repo(tmp_path)
    opened = await daemon._control("workspace.open", {"path": str(repo)})
    daemon.bridge.diff_resolver.resolve = lambda source, root: PATCH
    result = await daemon._control(
        "page.publish",
        {"workspace": opened["workspace"], "source": {"branch": ""}},
    )
    assert result["ok"] is True and result["rev"] == 1
    assert result["root"] == str(repo.resolve())
    # identical re-publish is idempotent: findings stay current
    again = await daemon._control(
        "page.publish",
        {"workspace": opened["workspace"], "source": {"branch": ""}},
    )
    assert again["page_id"] == result["page_id"] and again["rev"] == 1


async def test_page_publish_doc_without_agent(daemon, tmp_path):
    repo = make_repo(tmp_path)
    opened = await daemon._control("workspace.open", {"path": str(repo)})
    result = await daemon._control(
        "page.publish",
        {
            "workspace": opened["workspace"],
            "type": "doc",
            "name": "Plan",
            "content": "# plan",
        },
    )
    page = daemon.workspaces.page(result["page_id"])
    assert page is not None and page.data["content"] == "# plan"
    assert daemon.registry.all() == []


async def test_page_publish_errors_carry_context(daemon, tmp_path):
    with pytest.raises(ValueError, match="unknown workspace"):
        await daemon._control(
            "page.publish", {"workspace": "box:/nope", "source": {"staged": True}}
        )
    repo = make_repo(tmp_path)
    opened = await daemon._control("workspace.open", {"path": str(repo)})
    with pytest.raises(ValueError, match="source must be"):
        await daemon._control(
            "page.publish", {"workspace": opened["workspace"], "source": {"x": 1}}
        )
    from voco.adapters.diffsource import DiffResolveError

    def boom(source, root):
        raise DiffResolveError("no merge-base")

    daemon.bridge.diff_resolver.resolve = boom
    with pytest.raises(ValueError, match="workspace root"):
        await daemon._control(
            "page.publish", {"workspace": opened["workspace"], "source": {"branch": ""}}
        )


def test_new_commands_are_legal_envelopes():
    for cmd in (
        "workspace.open",
        "workspace.register",
        "page.publish",
        "session.transcript",
    ):
        env = validate_envelope({"cmd": cmd, "payload": {}})
        assert env.type == cmd


async def test_session_transcript_command_by_name(daemon):
    s = daemon.registry.register(IDENT, ["say"])
    daemon.registry.dispatch("hello there", "t-1")
    result = await daemon._control("session.transcript", {"name": s.call_name})
    assert [line["text"] for line in result["inputs"]] == ["hello there"]
    with pytest.raises(ValueError, match="no session named"):
        await daemon._control("session.transcript", {"name": "Nobody"})
