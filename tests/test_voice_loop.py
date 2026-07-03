"""Headless end-to-end pipeline test: frames → VAD → machine → STT → route
→ dispatch, with every impure edge injected (VoiceLoopDeps).

This is the coverage that protects live-audio validation: the whole voice
pipeline runs in-process with a scripted VAD model, canned STT, a fake
player, and fast turn timings.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from voco.core.events import EventBus
from voco.core.phrases import PhraseCommand
from voco.core.router import Routed
from voco.core.turn import RouteDecision, TurnState
from voco.core.vad import FRAME_SAMPLES
from voco.voice_loop import VoiceLoop, VoiceLoopDeps


class FakeTts:
    voice = "test"
    sample_rate = 24_000

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def synth_bytes(self, text: str) -> bytes:
        return b"\x00\x00" * 240  # 10ms of silence

    def stream(self, text: str):
        async def chunks():
            yield b"\x00\x00" * 240

        return chunks()


class FakeStt:
    def __init__(self, canned: str) -> None:
        self.canned = canned

    def transcribe(self, pcm: bytes) -> str:
        return self.canned if pcm else ""


class FakeMic:
    def __init__(self, on_frame, device=None) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


class FakePlayer:
    """Records items; playback 'finishes' only when the test pumps it."""

    def __init__(self, on_finished, on_playing_changed, **kwargs: Any) -> None:
        self.on_finished = on_finished
        self.items: list[Any] = []
        self.stops = 0

    def bind_loop(self, loop) -> None:
        pass

    def play(self, item) -> None:
        self.items.append(item)

    def stop(self) -> None:
        self.stops += 1

    def finish_current(self) -> None:
        self.on_finished()


class ScriptedVad:
    """frame value >= 500 counts as speech (prob 0.9), else silence."""

    def __call__(self, frame: np.ndarray) -> float:
        return 0.9 if int(frame[0]) >= 500 else 0.1


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
