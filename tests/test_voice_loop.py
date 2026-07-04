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


def make_loop(tmp_path, canned="run the tests", attention="always"):
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
    deps = VoiceLoopDeps(
        load_vad_model=lambda path: ScriptedVad(),
        stt_builder=lambda provider, **kw: FakeStt(canned),
        tts_factory=FakeTts,
        mic_factory=FakeMic,
        player_factory=FakePlayer,
        hotkey_factory=None,  # no global hooks in tests
    )
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
        assert text == "run the tests" and decision.kind == "forward"
        assert voice.machine.state is TurnState.IDLE  # dispatch closed turn
        assert ("stt.final", {"text": "run the tests"}) in events
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
