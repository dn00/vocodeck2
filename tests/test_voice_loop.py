"""Headless end-to-end pipeline test: frames → VAD → machine → STT → route
→ dispatch, with every impure edge injected (VoiceLoopDeps).

This is the coverage that protects live-audio validation: the whole voice
pipeline runs in-process with a scripted VAD model, canned STT, a fake
player, and fast turn timings.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from fakes import FakeMic, FakePlayer, FakeStt, FakeTts, ScriptedVad
from voco.core.arbitration import DuplexMode
from voco.core.events import EventBus
from voco.core.phrases import PhraseCommand
from voco.core.router import Routed
from voco.core.turn import RouteDecision, TurnState
from voco.core.vad import FRAME_SAMPLES
from voco.voice_loop import VoiceLoop, VoiceLoopDeps


class Host:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, RouteDecision]] = []
        self.phrases: list[PhraseCommand] = []
        self.route_kind = "forward"

    async def route(self, text: str) -> Routed:
        return Routed(decision=RouteDecision(kind=self.route_kind))

    def run_phrase(self, cmd: PhraseCommand) -> None:
        self.phrases.append(cmd)

    def dispatch(self, text: str, decision: RouteDecision) -> tuple[str, str]:
        self.dispatched.append((text, decision))
        return f"t-{len(self.dispatched)}", "live"


CFG = {
    "audio": {
        # Fast turn timings so the test completes in ~100ms of real time.
        "dispatch_hold_ms": 40,
        "reopen_window_ms": 80,
        "incomplete_hold_ms": 100,
        "min_speech_ms": 96,  # 3 frames
        "min_silence_ms": 64,  # 2 frames
        "phrase_bank_dir": None,  # replaced per-test with tmp_path
    },
    "stt": {"provider": "fake"},
    "tts": {"base_url": "http://none", "model": "x", "voice": "test"},
}


def speech_frame() -> np.ndarray:
    return np.full(FRAME_SAMPLES, 1000, dtype=np.int16)


def silence_frame() -> np.ndarray:
    return np.full(FRAME_SAMPLES, 0, dtype=np.int16)


# Canned transcript is punctuated like real Whisper output for a finished
# utterance — unpunctuated text now triggers incomplete_hold_ms patience.
def make_loop(tmp_path, canned="run the tests.", attention="always", **dep_overrides):
    bus = EventBus()
    events: list = []
    bus.subscribe(lambda env: events.append((env.type, env.payload)))
    host = Host()
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "phrase_bank_dir": str(tmp_path / "bank"),
            "attention": attention,
        },
    }
    dep_args: dict = dict(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt(canned),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,  # no global hooks in tests
    )
    dep_args.update(dep_overrides)
    deps = VoiceLoopDeps(**dep_args)
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    return voice, host, events


async def feed(voice: VoiceLoop, frames: int, kind) -> None:
    for _ in range(frames):
        voice._process_frame(kind())
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_full_pipeline_speech_to_dispatch(tmp_path):
    voice, host, events = make_loop(tmp_path)
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 4, speech_frame)  # ≥96ms: opens the turn
        assert voice.machine.state is TurnState.CAPTURING
        await feed(voice, 3, silence_frame)  # ≥64ms: closes capture
        assert voice.machine.state is TurnState.HOLDING
        player: FakePlayer = voice.player  # type: ignore[assignment]
        assert any(i.source.value == "ack" for i in player.items)  # chirp
        # STT (canned) + route (forward) + hold expiry → dispatch.
        for _ in range(50):
            if host.dispatched:
                break
            await asyncio.sleep(0.02)
        assert host.dispatched, "pipeline never dispatched"
        text, decision = host.dispatched[0]
        assert text == "run the tests." and decision.kind == "forward"
        assert voice.machine.state is TurnState.IDLE  # dispatch closed turn
        assert ("stt.final", {"text": "run the tests."}) in events
    finally:
        voice.stop()


async def wait_for(predicate, tries: int = 50) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_route_failure_degrades_to_forward_verbatim(tmp_path):
    """A dead router (mate down, bug) must not strand the turn in ROUTING
    with no deadline — the utterance forwards verbatim (SPEC §6 floor)."""
    voice, host, events = make_loop(tmp_path)

    async def broken_route(text: str) -> Routed:
        raise RuntimeError("mate exploded")

    host.route = broken_route  # type: ignore[method-assign]
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 4, speech_frame)
        await feed(voice, 3, silence_frame)
        await wait_for(lambda: host.dispatched)
        assert host.dispatched, "turn stranded in ROUTING"
        text, decision = host.dispatched[0]
        assert text == "run the tests." and decision.kind == "forward"
        assert any(t == "daemon.error" for t, _ in events)
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_dispatch_edge_failure_does_not_kill_the_pump(tmp_path):
    """A raising dispatch edge closes the turn (machine one-way door) and
    is caught by the deadline pump: the NEXT utterance still dispatches."""
    voice, host, events = make_loop(tmp_path)
    real = host.dispatch
    calls = {"n": 0}

    def flaky(text, decision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("registry hiccup")
        return real(text, decision)

    host.dispatch = flaky  # type: ignore[method-assign]
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 4, speech_frame)
        await feed(voice, 3, silence_frame)
        # NB: start() already emits a daemon.error for the disabled PTT
        # hotkey — wait on the flaky call itself, not on any error.
        await wait_for(lambda: calls["n"] >= 1)
        assert calls["n"] == 1
        assert any(
            t == "daemon.error" and "registry hiccup" in str(p) for t, p in events
        )
        assert voice.machine.state is TurnState.IDLE
        assert not host.dispatched
        await feed(voice, 4, speech_frame)
        await feed(voice, 3, silence_frame)
        await wait_for(lambda: host.dispatched)
        assert host.dispatched, "pump died after the first failure"
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_incomplete_transcript_emits_turn_patience(tmp_path):
    """The deliberate wait is visible on the wire: UI/live tests can tell
    'waiting for the user to finish' from 'stuck'."""
    voice, host, events = make_loop(tmp_path, canned="one is")
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 4, speech_frame)
        await feed(voice, 3, silence_frame)
        await wait_for(lambda: host.dispatched)
        assert host.dispatched  # patience expired -> fragment dispatched
        patience = [p for t, p in events if t == "turn.patience"]
        assert patience and patience[0]["wait_ms"] >= 0
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_no_onset_clipping_with_production_entry_threshold(tmp_path):
    """Live-test bug: the first 1-2 words were lost. With the production
    384ms entry bar (12 frames > the old 10-frame pre-roll), every speech
    frame — including the very first — must reach STT via pre-roll."""
    bus = EventBus()
    host = Host()
    stt = FakeStt("hi")
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "min_speech_ms": 384,
            "phrase_bank_dir": str(tmp_path / "bank"),
        },
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: stt,
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
    )
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    await voice.start(asyncio.get_running_loop())
    try:
        # Distinct first samples mark each frame (>=500 scores as speech).
        for i in range(14):
            voice._process_frame(np.full(FRAME_SAMPLES, 1000 + i, dtype=np.int16))
        await asyncio.sleep(0)
        assert voice.machine.state is TurnState.CAPTURING
        await feed(voice, 3, silence_frame)  # close the capture
        for _ in range(50):
            if stt.received:
                break
            await asyncio.sleep(0.02)
        assert stt.received, "utterance never reached STT"
        pcm = np.frombuffer(stt.received[0], dtype=np.int16)
        marks = set(pcm[::FRAME_SAMPLES].tolist())
        missing = {1000 + i for i in range(14)} - marks
        assert not missing, f"clipped frames at utterance start: {sorted(missing)}"
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_muted_attention_blocks_the_pipeline(tmp_path):
    voice, host, _events = make_loop(tmp_path, attention="muted")
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 10, speech_frame)
        assert voice.machine.state is TurnState.IDLE  # nothing opens
        await asyncio.sleep(0.1)
        assert not host.dispatched
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_wake_mode_arms_on_scorer_and_then_dispatches(tmp_path):
    bus = EventBus()
    host = Host()
    woken = {"score": 0.0}
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "phrase_bank_dir": str(tmp_path / "bank"),
            "attention": "wake",
            "wake_model": "fake.onnx",
            "wake_window_s": 5.0,
        },
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt("hello there"),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
        wake_loader=lambda path: lambda frame: woken["score"],
    )
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    await voice.start(asyncio.get_running_loop())
    try:
        await feed(voice, 6, speech_frame)  # speech, but not woken
        assert voice.machine.state is TurnState.IDLE
        await feed(voice, 3, silence_frame)  # VAD closes its speech run
        woken["score"] = 0.9  # "voco"
        await feed(voice, 1, silence_frame)  # scorer fires, window arms
        assert voice.attention.allows_vad()
        await feed(voice, 4, speech_frame)
        assert voice.machine.state is TurnState.CAPTURING
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_half_duplex_switch_mid_playback_suppresses_immediately(tmp_path):
    """Echo rescue (live-test): flipping to half_duplex WHILE the bot is
    speaking must deafen the gate now, not at the next playback edge."""
    voice, _host, _events = make_loop(tmp_path)
    await voice.start(asyncio.get_running_loop())
    try:
        voice._on_playing_changed(True)  # bot speaking, full duplex: gate open
        assert not voice.vad_gate.suppressed
        voice.set_duplex(DuplexMode.HALF)
        assert voice.vad_gate.suppressed
        voice.set_duplex(DuplexMode.FULL)  # back: barge-in wants the gate open
        assert not voice.vad_gate.suppressed
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_half_duplex_playback_end_drops_echo_pre_roll(tmp_path):
    """The pre-roll ring buffers our own speaker tail while suppressed;
    the next utterance must open with user audio only."""
    bus = EventBus()
    host = Host()
    stt = FakeStt("hi")
    cfg = {
        **CFG,
        "audio": {**CFG["audio"], "phrase_bank_dir": str(tmp_path / "bank")},
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: stt,
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
    )
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    voice.set_duplex(DuplexMode.HALF)
    await voice.start(asyncio.get_running_loop())
    try:
        voice._on_playing_changed(True)
        for i in range(5):  # speaker echo: loud frames, marked 2000+
            voice._process_frame(np.full(FRAME_SAMPLES, 2000 + i, dtype=np.int16))
        assert voice.machine.state is TurnState.IDLE  # suppressed
        voice._on_playing_changed(False)  # playback ends → ring dropped
        for i in range(4):  # user speaks, marked 1000+
            voice._process_frame(np.full(FRAME_SAMPLES, 1000 + i, dtype=np.int16))
        await asyncio.sleep(0)
        assert voice.machine.state is TurnState.CAPTURING
        await feed(voice, 3, silence_frame)
        for _ in range(50):
            if stt.received:
                break
            await asyncio.sleep(0.02)
        assert stt.received
        pcm = np.frombuffer(stt.received[0], dtype=np.int16)
        marks = set(pcm[::FRAME_SAMPLES].tolist())
        assert not {2000 + i for i in range(5)} & marks, "bot echo reached STT"
        assert 1000 in marks  # the user's first frame survived
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_wake_scorer_deaf_during_half_duplex_playback(tmp_path):
    bus = EventBus()
    host = Host()
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "phrase_bank_dir": str(tmp_path / "bank"),
            "duplex": "half_duplex",
            "attention": "wake",
            "wake_model": "fake.onnx",
        },
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt("x"),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
        wake_loader=lambda path: lambda frame: 0.9,  # always "voco"
    )
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    await voice.start(asyncio.get_running_loop())
    try:
        voice._on_playing_changed(True)  # TTS says something voco-like
        await feed(voice, 3, silence_frame)
        assert not voice.attention.allows_vad()  # did not self-wake
        voice._on_playing_changed(False)
        await feed(voice, 1, silence_frame)
        assert voice.attention.allows_vad()  # user can wake it again
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_speech_synthesizes_sentence_by_sentence(tmp_path):
    """Triage: TTFA must not scale with message length — one playback
    item, per-sentence synth calls; the mate speaks with its own voice."""
    bus = EventBus()
    host = Host()
    cfg = {
        **CFG,
        "audio": {**CFG["audio"], "phrase_bank_dir": str(tmp_path / "bank")},
        "first_mate": {"base_url": "http://none", "voice": "af_sky"},
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt(""),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
    )
    voice = VoiceLoop(cfg, bus, host=host, deps=deps)
    voice.speak_agent("First part. Second part! Third?", turn_id=None)
    voice.speak_local("Mate here. Two lines.", turn_id=None)
    player: FakePlayer = voice.player  # type: ignore[assignment]
    assert len(player.items) == 1  # agent plays; mate queued behind it
    async for _ in player.items[0].content:  # drain drives per-sentence synth
        pass
    player.finish_current()  # queue pumps the mate item next
    assert len(player.items) == 2
    async for _ in player.items[1].content:
        pass
    tts: FakeTts = voice.tts  # type: ignore[assignment]
    assert tts.synthesized == [
        ("First part.", None),  # agent: shared voice
        ("Second part!", None),
        ("Third?", None),
        ("Mate here.", "af_sky"),  # mate: its own voice
        ("Two lines.", "af_sky"),
    ]


@pytest.mark.asyncio
async def test_full_duplex_speech_barges_in_on_playback(tmp_path):
    voice, _host, events = make_loop(tmp_path)
    await voice.start(asyncio.get_running_loop())
    try:
        voice.speak_agent("long agent monologue", turn_id=None)
        player: FakePlayer = voice.player  # type: ignore[assignment]
        assert player.items[-1].source.value == "agent"
        await feed(voice, 4, speech_frame)  # user talks over it
        assert player.stops == 1  # rule 1: flushed
        assert any(t == "speech.interrupted" for t, _ in events)
    finally:
        voice.stop()


# ---- PTT permission preflight (BUILD-PROD P4) --------------------------------


class FakePtt:
    """Constructs and starts cleanly — preflight only runs after a
    successful start, so the disabled-factory default can't reach it."""

    def __init__(self, loop, *, on_press, on_release, key):
        self.key = key

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_ptt_denied_permission_reaches_the_deck(tmp_path):
    """pynput never raises on a missing macOS Input Monitoring grant —
    it warns to stderr and PTT silently does nothing. The preflight
    turns that into an actionable daemon.error the deck can toast."""
    voice, _host, events = make_loop(
        tmp_path, hotkey_factory=FakePtt, ptt_preflight=lambda: False
    )
    await voice.start(asyncio.get_running_loop())
    try:
        denied = [
            p
            for t, p in events
            if t == "daemon.error" and "Input Monitoring" in p.get("error", "")
        ]
        assert denied, "denied preflight never reached the bus"
        assert "restart voco-d" in denied[0]["error"]  # actionable, not vague
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_ptt_unknown_permission_is_not_denied(tmp_path):
    # None = "cannot tell" (non-mac, missing symbol) — the loop must
    # NOT cry wolf about permissions it cannot actually check
    voice, _host, events = make_loop(
        tmp_path, hotkey_factory=FakePtt, ptt_preflight=lambda: None
    )
    await voice.start(asyncio.get_running_loop())
    try:
        assert not [
            p
            for t, p in events
            if t == "daemon.error" and "Input Monitoring" in p.get("error", "")
        ]
    finally:
        voice.stop()


# ---- wake word (P12: wiring + honesty) ----------------------------------------


def test_wake_loader_default_is_the_real_detector():
    """THE P12 regression pin: wake_loader defaulted to None and nothing
    ever wired it — the adapter was dead code and wake mode a silent
    lie. The real loader is now the default (lazy import inside)."""
    from voco.adapters.wake import load_openwakeword

    assert VoiceLoopDeps().wake_loader is load_openwakeword


@pytest.mark.asyncio
async def test_wake_mode_without_model_falls_back_honestly(tmp_path):
    from voco.core.attention import AttentionMode

    voice, _host, events = make_loop(tmp_path, attention="wake", wake_loader=None)
    await voice.start(asyncio.get_running_loop())
    try:
        assert voice.attention.mode is AttentionMode.PTT_ONLY  # not deaf-wake
        errs = [
            p["error"]
            for t, p in events
            if t == "daemon.error" and "wake attention unavailable" in p["error"]
        ]
        assert errs and "wake_model" in errs[0]  # names the fix
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_wake_model_load_failure_names_the_missing_extra(tmp_path):
    from voco.core.attention import AttentionMode

    def loader_raises(path):
        raise ImportError("No module named 'openwakeword'")

    bus = EventBus()
    events: list = []
    bus.subscribe(lambda env: events.append((env.type, env.payload)))
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "phrase_bank_dir": str(tmp_path / "bank"),
            "attention": "wake",
            "wake_model": "models/voco.onnx",
        },
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt("x"),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
        wake_loader=loader_raises,
    )
    voice = VoiceLoop(cfg, bus, host=Host(), deps=deps)
    await voice.start(asyncio.get_running_loop())
    try:
        assert voice.attention.mode is AttentionMode.PTT_ONLY
        errs = [p["error"] for t, p in events if t == "daemon.error"]
        assert any("--extra wake" in e for e in errs)  # actionable reason
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_runtime_switch_to_wake_without_detector_is_refused(tmp_path):
    from voco.core.attention import AttentionMode

    voice, _host, events = make_loop(tmp_path, wake_loader=None)  # always mode
    await voice.start(asyncio.get_running_loop())
    try:
        assert voice.set_attention(AttentionMode.WAKE) is False  # refused
        assert voice.attention.mode is AttentionMode.ALWAYS  # unchanged
        assert any(
            t == "daemon.error" and "wake attention unavailable" in p["error"]
            for t, p in events
        )
        # a mode with a WORKING path still switches (the refusal is
        # wake-specific, not a frozen control)
        assert voice.set_attention(AttentionMode.MUTED) is True
        assert voice.attention.mode is AttentionMode.MUTED
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_muted_stays_muted_when_wake_is_refused(tmp_path):
    # Review pin: only always->wake was covered. A MUTED deck whose wake
    # detector can't arm must NOT slide into wake on a refused switch —
    # it stays muted, and a mode with a working path still applies.
    from voco.core.attention import AttentionMode

    voice, _host, _events = make_loop(tmp_path, attention="muted", wake_loader=None)
    await voice.start(asyncio.get_running_loop())
    try:
        assert voice.set_attention(AttentionMode.WAKE) is False
        assert voice.attention.mode is AttentionMode.MUTED  # not deaf-wake
        assert voice.set_attention(AttentionMode.PTT_ONLY) is True
        assert voice.attention.mode is AttentionMode.PTT_ONLY
    finally:
        voice.stop()


@pytest.mark.asyncio
async def test_wake_model_set_but_loader_absent_names_the_build(tmp_path):
    # wake_model IS configured but no loader is wired: the reason must not
    # tell the user to set a key they already set — it names the build.
    from voco.core.attention import AttentionMode

    bus = EventBus()
    events: list = []
    bus.subscribe(lambda env: events.append((env.type, env.payload)))
    cfg = {
        **CFG,
        "audio": {
            **CFG["audio"],
            "phrase_bank_dir": str(tmp_path / "bank"),
            "attention": "wake",
            "wake_model": "models/voco.onnx",
        },
    }
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt("x"),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,
        wake_loader=None,
    )
    voice = VoiceLoop(cfg, bus, host=Host(), deps=deps)
    await voice.start(asyncio.get_running_loop())
    try:
        assert voice.attention.mode is AttentionMode.PTT_ONLY  # honest degrade
        errs = [
            p["error"]
            for t, p in events
            if t == "daemon.error" and "wake attention unavailable" in p["error"]
        ]
        assert errs
        assert "set [audio].wake_model" not in errs[0]  # the config IS set
        assert "disabled in this build" in errs[0]  # names the real miss
    finally:
        voice.stop()


# ---- mic.level (index7 honest meters) ------------------------------------------


def test_mic_level_is_throttled_and_settles_to_one_zero(tmp_path):
    """The deck meter reads SIGNAL: ~10Hz while the level moves, one
    trailing zero at silence, then nothing — never an idle event storm."""
    voice, _host, events = make_loop(tmp_path)
    loud = np.full(512, 6000, dtype=np.int16)
    voice._level_ts = 0.0
    voice._emit_mic_level(loud)
    levels = [p["level"] for t, p in events if t == "mic.level"]
    assert levels and 0.2 < levels[-1] <= 1.0
    n_before = len(levels)
    voice._emit_mic_level(loud)  # within the 100ms window: throttled
    assert len([1 for t, _p in events if t == "mic.level"]) == n_before
    quiet = np.zeros(512, dtype=np.int16)
    voice._level_ts = 0.0
    voice._emit_mic_level(quiet)
    voice._level_ts = 0.0
    voice._emit_mic_level(quiet)  # silence repeats are suppressed
    levels = [p["level"] for t, p in events if t == "mic.level"]
    assert levels[-1] == 0.0
    assert levels.count(0.0) == 1
