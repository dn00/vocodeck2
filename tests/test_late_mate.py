"""Mate off the critical path (triage 2026-07-03): a deadline miss no
longer cancels the mate — dispatch goes with the fast path and the late
decision still acts, speaks (TTL/rule-3 policed), and corrects misroutes.
"""

from __future__ import annotations

import asyncio

from fakes import FakeMic, FakePlayer, FakeStt, FakeTts, ScriptedVad
from voco.core.arbitration import PlaybackItem, PlaybackQueue, Source
from voco.core.router import Routed, Router
from voco.core.turn import RouteDecision
from voco.daemon import Daemon
from voco.voice_loop import VoiceLoop, VoiceLoopDeps


class SlowMate:
    def __init__(self, decision: RouteDecision | None, delay: float = 0.05) -> None:
        self._decision = decision
        self._delay = delay

    async def route(self, text: str, grounding: dict) -> RouteDecision | None:
        await asyncio.sleep(self._delay)
        if isinstance(self._decision, Exception):
            raise self._decision
        return self._decision


# ---- router --------------------------------------------------------------


async def test_timeout_returns_fast_path_and_reports_late():
    late: list[RouteDecision | None] = []
    mate_says = RouteDecision(kind="ack_forward", speech="on it", target="Marcus")
    router = Router(first_mate=SlowMate(mate_says), timeout_s=0.01)
    routed = await router.decide(
        "tell Marcus to go", ["Marcus"], {}, on_late=late.append
    )
    assert routed.late_pending
    assert routed.decision is not None and routed.decision.kind == "forward"
    assert routed.decision.target == "Marcus"  # fallback guard still applies
    await asyncio.sleep(0.1)
    assert late and late[0] is mate_says


async def test_fast_mate_never_reports_late():
    late: list = []
    fast = SlowMate(RouteDecision(kind="answer", speech="two sessions"), delay=0.0)
    router = Router(first_mate=fast, timeout_s=1.0)
    routed = await router.decide("how many sessions", [], {}, on_late=late.append)
    assert routed.decision is not None and routed.decision.kind == "answer"
    assert not routed.late_pending
    await asyncio.sleep(0.05)
    assert late == []


async def test_late_mate_failure_reports_none():
    late: list = []
    router = Router(first_mate=SlowMate(RuntimeError("boom")), timeout_s=0.01)
    routed = await router.decide("hello there", [], {}, on_late=late.append)
    assert routed.late_pending
    await asyncio.sleep(0.1)
    assert late == [None]


async def test_no_late_callback_keeps_cancel_semantics():
    # Without on_late (calibration scripts etc.) the old contract holds.
    router = Router(first_mate=SlowMate(None, delay=5.0), timeout_s=0.01)
    routed = await router.decide("hello", [], {})
    assert isinstance(routed, Routed) and not routed.late_pending


# ---- daemon integration ------------------------------------------------------


def ident(cwd: str) -> dict:
    return {"host": "mac", "user": "dn", "cwd": cwd, "harness": "claude"}


def make_daemon(tmp_path) -> Daemon:
    cfg = {
        "audio": {"phrase_bank_dir": str(tmp_path / "bank")},
        "stt": {"provider": "fake"},
        "tts": {"base_url": "http://none", "model": "x", "voice": "test"},
        "state": {"dir": str(tmp_path / "state")},
    }
    d = Daemon(cfg, no_audio=True, config_path=tmp_path / "config.toml")
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt(""),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
    )
    d.voice = VoiceLoop(cfg, d.bus, host=d, deps=deps)
    return d


async def test_late_ack_speaks_with_the_dispatched_turn(tmp_path):
    d = make_daemon(tmp_path)
    d.registry.register(ident("/repo/a"), ["say", "listen"])  # active
    d.router = Router(
        first_mate=SlowMate(RouteDecision(kind="ack_forward", speech="on it")),
        timeout_s=0.01,
    )
    routed = await d.route("run the tests please")
    assert routed.late_pending
    turn_id, _ = d.dispatch("run the tests please", routed.decision)
    await asyncio.sleep(0.1)
    player = d.voice.player  # type: ignore[union-attr]
    mate = [i for i in player.items if i.source is Source.FIRST_MATE]
    assert mate, "late ack never reached playback"
    assert mate[-1].turn_id == turn_id  # rule-2/3 attribution


async def test_late_reroute_redispatches_and_says_so(tmp_path):
    d = make_daemon(tmp_path)
    a = d.registry.register(ident("/repo/a"), ["say", "listen"])  # active
    b = d.registry.register(ident("/repo/b"), ["say", "listen"])
    events: list = []
    d.bus.subscribe(lambda env: events.append((env.type, env.payload)))
    d.router = Router(
        first_mate=SlowMate(RouteDecision(kind="forward", target=b.call_name)),
        timeout_s=0.01,
    )
    routed = await d.route("check the build")
    d.dispatch("check the build", routed.decision)  # fast path: lands on a
    assert [q.text for q in a.queued] == ["check the build"]
    await asyncio.sleep(0.1)
    # Late mate corrected the destination: b got the words too.
    assert [q.text for q in b.queued] == ["check the build"]
    assert any(t == "route.decision" and p.get("late_reroute") for t, p in events)
    player = d.voice.player  # type: ignore[union-attr]
    assert any(i.source is Source.FIRST_MATE for i in player.items)  # spoken


async def test_late_action_executes(tmp_path):
    d = make_daemon(tmp_path)
    d.registry.register(ident("/repo/a"), ["say", "listen"])
    b = d.registry.register(ident("/repo/b"), ["say", "listen"])
    d.router = Router(
        first_mate=SlowMate(
            RouteDecision(
                kind="answer",
                speech="switching",
                action={"type": "switch_session", "target": b.call_name},
            )
        ),
        timeout_s=0.01,
    )
    routed = await d.route(f"go to {b.call_name} please")
    assert routed.late_pending
    await asyncio.sleep(0.1)
    assert d.registry.active is b  # idempotent deck op, fine late


# ---- arbitration TTL (rule 6) -----------------------------------------------


class RecPlayer:
    def __init__(self) -> None:
        self.played: list[PlaybackItem] = []

    def play(self, item: PlaybackItem) -> None:
        self.played.append(item)

    def stop(self) -> None:
        pass


def test_stale_mate_speech_expires_instead_of_narrating():
    clock = {"t": 0.0}
    events: list = []
    player = RecPlayer()
    q = PlaybackQueue(
        player, emit=lambda t, p: events.append((t, p)), now=lambda: clock["t"]
    )
    q.set_gate(True)  # user speaking: nothing may start
    q.enqueue(PlaybackItem(Source.FIRST_MATE, b"stale-ack"))
    clock["t"] += 7.0  # gate stayed shut past the TTL
    q.set_gate(False)
    assert player.played == []  # never narrated the stale moment
    assert any(t == "speech.expired" for t, _ in events)
    q.enqueue(PlaybackItem(Source.FIRST_MATE, b"fresh"))
    assert [i.content for i in player.played] == [b"fresh"]
