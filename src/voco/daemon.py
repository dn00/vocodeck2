"""voco-d — composition root (SPEC §2, §11).

ROLE: load config, wire ports to adapters (mic→VAD→turn machine→router→
registry/bridge; TTS→arbitration→speakers), run the HTTP/WS site and the
audio loop. All policy lives in core/; this module only composes.

INVARIANTS: --no-audio runs bridge+core only (headless bring-up, CI);
provider construction failures degrade the capability and keep the daemon
up (fail-silent toward agents, loud on the event bus).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from voco.bridge.http import BridgeServer, run_server
from voco.core.arbitration import DuplexMode, PlaybackItem, PlaybackQueue, Source
from voco.core.events import EventBus
from voco.core.registry import Registry
from voco.core.router import Router
from voco.core.turn import RouteDecision, TurnConfig, TurnEvents, TurnMachine, TurnState

DEFAULT_CONFIG = Path.home() / ".config" / "voco" / "config.toml"


def load_config(path: Path | None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    if p.exists():
        return tomllib.loads(p.read_text())
    return {}


class Daemon:
    def __init__(self, cfg: dict[str, Any], no_audio: bool = False) -> None:
        self.cfg = cfg
        self.no_audio = no_audio
        self.bus = EventBus()
        self.registry = Registry(emit=self.bus.emit)
        self.router = Router(llm=None)  # Gemma tier lands in M1
        self.bridge = BridgeServer(
            self.registry,
            self.bus,
            token=cfg.get("bridge", {}).get("token"),
            on_control=self._control,
        )
        self.duplex = DuplexMode(
            cfg.get("audio", {}).get("duplex", DuplexMode.FULL.value)
        )
        self.loop: asyncio.AbstractEventLoop | None = None

        # Audio members are filled by _wire_audio (skipped in --no-audio).
        self.machine: TurnMachine | None = None
        self.playback_queue: PlaybackQueue | None = None
        self._deadline_wakeup = asyncio.Event()
        self._speculation: dict[tuple[int, int], asyncio.Task] = {}
        self._ptt = None

    # ---- control commands (CLI/WS) ------------------------------------------

    def _control(self, cmd: str, payload: dict) -> dict:
        if cmd == "switch_session":
            s = self.registry.switch(str(payload.get("name", "")))
            if s is None:
                raise ValueError(f"no session named {payload.get('name')!r}")
            return {"active": s.session_id, "name": s.call_name}
        if cmd == "interrupt":
            if self.playback_queue is not None:
                self.playback_queue.barge_in()
            return {}
        if cmd == "mic.set":
            mode = str(payload.get("mode", ""))
            if mode in (DuplexMode.FULL.value, DuplexMode.HALF.value):
                self.duplex = DuplexMode(mode)
                if self.playback_queue is not None:
                    self.playback_queue.set_duplex(self.duplex)
                self.bus.emit("mic.state", {"mode": mode})
                return {"mode": mode}
            raise ValueError(f"unknown mic mode {mode!r}")
        if cmd == "say_as_user":
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("text required")
            asyncio.get_running_loop().create_task(self._route_and_dispatch(text))
            return {}
        if cmd in ("config.get", "config.set"):
            raise ValueError("config commands land in M3")
        raise ValueError(f"unknown command {cmd!r}")

    # ---- typed/text input path (audio path reuses this from stt_final) --------

    async def _route_and_dispatch(self, text: str) -> None:
        routed = await self.router.decide(text, self.registry.call_names(), {})
        if routed.phrase is not None:
            self._run_phrase(routed.phrase)
            return
        decision = routed.decision or RouteDecision(kind="forward")
        turn_id = self.registry.mint_turn_id()
        self.bus.emit(
            "route.decision",
            {"turn_id": turn_id, "kind": decision.kind, "text": text},
        )
        target = (
            self.registry.by_call_name(decision.target) if decision.target else None
        )
        result = self.registry.dispatch(text, turn_id, target=target)
        if self.playback_queue is not None:
            self.playback_queue.note_dispatch(turn_id)
            if result in ("no_session", "queued_idle"):
                self._play_bank("line-dead")

    def _run_phrase(self, cmd) -> None:
        if cmd.kind == "stop" and self.playback_queue is not None:
            self.playback_queue.barge_in()
        elif cmd.kind == "switch" and cmd.target:
            self.registry.switch(cmd.target)
        elif cmd.kind in ("mute", "unmute"):
            self.bus.emit("mic.state", {"mode": cmd.kind})
            if self._vad_gate is not None:
                self._vad_gate.suppress(cmd.kind == "mute")

    def _play_bank(self, key: str) -> None:
        if self.playback_queue is None or self._bank is None:
            return
        pcm = self._bank.get(key)
        if pcm:
            self.playback_queue.enqueue(
                PlaybackItem(
                    Source.ACK, pcm, duration_ms=self._bank.duration_ms(key)
                )
            )

    # ---- audio wiring -----------------------------------------------------------

    _vad_gate = None
    _bank = None
    _capture = None
    _mic = None
    _player = None
    _stt = None
    _tts = None

    def _wire_audio(self) -> None:
        import numpy as np  # noqa: F401

        from voco.audio.capture import CaptureBuffer, MicStream
        from voco.audio.playback import SpeakerPlayer
        from voco.audio.vad import VadConfig, VadGate, load_silero
        from voco.providers.stt import build_stt
        from voco.providers.tts import OpenAICompatibleTts, PhraseBank

        audio_cfg = self.cfg.get("audio", {})
        stt_cfg = self.cfg.get("stt", {"provider": "faster-whisper"})
        tts_cfg = self.cfg.get(
            "tts",
            {
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "kokoro",
                "voice": "af_heart",
            },
        )

        self._tts = OpenAICompatibleTts(
            base_url=tts_cfg["base_url"],
            model=tts_cfg["model"],
            voice=tts_cfg["voice"],
            sample_rate=int(tts_cfg.get("sample_rate", 24_000)),
            api_key=tts_cfg.get("api_key"),
        )
        cache = Path(
            audio_cfg.get(
                "phrase_bank_dir", Path.home() / ".cache" / "voco" / "phrase-bank"
            )
        )
        self._bank = PhraseBank(self._tts, cache)

        provider = dict(stt_cfg)
        self._stt = build_stt(provider.pop("provider"), **provider)

        self._player = SpeakerPlayer(
            on_finished=lambda: self.playback_queue.on_item_finished(),
            on_playing_changed=self._on_playing_changed,
            sample_rate=self._tts.sample_rate,
            device=audio_cfg.get("output_device"),
        )
        self.playback_queue = PlaybackQueue(self._player, emit=self.bus.emit)
        self.playback_queue.set_duplex(self.duplex)

        self.machine = TurnMachine(
            TurnEvents(
                capture_started=self._on_capture_started,
                capture_stopped=self._on_capture_stopped,
                chirp_requested=self._on_chirp,
                cancel_speculation=self._on_cancel,
                route_requested=self._on_route_requested,
                dispatch_ready=self._on_dispatch_ready,
                local_reply_ready=self._on_local_reply,
                turn_state_changed=self._on_turn_state,
            ),
            TurnConfig(
                dispatch_hold_ms=int(audio_cfg.get("dispatch_hold_ms", 800)),
                reopen_window_ms=int(audio_cfg.get("reopen_window_ms", 1200)),
            ),
            now=time.monotonic,
        )

        self._capture = CaptureBuffer()
        model_path = audio_cfg.get("silero_model", "models/silero_vad.onnx")
        self._vad_gate = VadGate(
            VadConfig(
                threshold=float(audio_cfg.get("vad_threshold", 0.5)),
                min_speech_ms=int(audio_cfg.get("min_speech_ms", 384)),
                min_speech_continuation_ms=int(
                    audio_cfg.get("min_speech_continuation_ms", 192)
                ),
                min_silence_ms=int(audio_cfg.get("min_silence_ms", 64)),
            ),
            model=load_silero(model_path),
            on_speech_started=self._on_vad_speech_start,
            on_speech_ended=lambda: self.machine.speech_ended(),
            reopenable=lambda: self.machine.state.value in ("holding", "reopenable"),
        )
        self._mic = MicStream(self._on_frame, device=audio_cfg.get("input_device"))

    # ---- audio-side callbacks (thread → loop marshaling at the edges) ------------

    def _on_frame(self, frame) -> None:
        # Called on the PortAudio thread: marshal onto the loop.
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self._process_frame, frame)

    def _process_frame(self, frame) -> None:
        self._capture.feed(frame)
        self._vad_gate.process(frame)

    def _on_vad_speech_start(self) -> None:
        if self.playback_queue is not None and self.duplex is DuplexMode.FULL:
            self.playback_queue.barge_in()  # rule 1
        self.machine.speech_started()

    def _on_playing_changed(self, playing: bool) -> None:
        # Half-duplex: deaf while we speak (+ grace handled by VAD reset).
        if self.duplex is DuplexMode.HALF and self._vad_gate is not None:
            self._vad_gate.suppress(playing)

    def _on_capture_started(self, key: int, reopened: bool) -> None:
        if reopened:
            self._capture.resume_utterance()
        else:
            self._capture.clear()
            self._capture.start_utterance()
        self._kick_deadline()

    def _on_capture_stopped(self, key: int, revision: int) -> None:
        self._capture.pause()
        pcm = self._capture.take()
        assert self.loop is not None
        task = self.loop.create_task(self._transcribe(key, revision, pcm))
        self._speculation[(key, revision)] = task
        self._kick_deadline()

    async def _transcribe(self, key: int, revision: int, pcm: bytes) -> None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, self._stt.transcribe, pcm)
        except Exception as e:
            self.bus.emit("daemon.error", {"error": f"stt: {e}"})
            text = ""
        self._speculation.pop((key, revision), None)
        self.bus.emit("stt.final", {"text": text})
        self.machine.stt_final(key, revision, text)
        self._kick_deadline()

    def _on_chirp(self, key: int) -> None:
        self._play_bank("chirp")

    def _on_cancel(self, key: int, revision: int) -> None:
        task = self._speculation.pop((key, revision), None)
        if task is not None:
            task.cancel()
        if self.playback_queue is not None:
            # Cancel speculative local speech; agent speech is never
            # speculative so flushing local sources is safe here.
            self.playback_queue.barge_in()

    def _on_route_requested(self, key: int, revision: int, text: str) -> None:
        assert self.loop is not None
        self.loop.create_task(self._route_turn(key, revision, text))

    async def _route_turn(self, key: int, revision: int, text: str) -> None:
        routed = await self.router.decide(text, self.registry.call_names(), {})
        if routed.phrase is not None:
            self._run_phrase(routed.phrase)
            # Phrase commands never dispatch: close out as a local answer
            # with no speech.
            self.machine.route_decided(
                key, revision, RouteDecision(kind="answer", speech="ok")
            )
            return
        self.machine.route_decided(
            key, revision, routed.decision or RouteDecision(kind="forward")
        )
        self._kick_deadline()

    def _on_dispatch_ready(self, key: int, text: str, decision: RouteDecision) -> None:
        turn_id = self.registry.mint_turn_id()
        self.bus.emit(
            "route.decision", {"turn_id": turn_id, "kind": decision.kind, "text": text}
        )
        target = (
            self.registry.by_call_name(decision.target) if decision.target else None
        )
        result = self.registry.dispatch(text, turn_id, target=target)
        if self.playback_queue is not None:
            self.playback_queue.note_dispatch(turn_id)
            if decision.kind == "ack_forward" and decision.speech:
                self._speak_local(decision.speech, turn_id)
            if result in ("no_session", "queued_idle"):
                self._play_bank("line-dead")

    def _on_local_reply(self, key: int, decision: RouteDecision) -> None:
        if decision.speech and decision.speech != "ok":
            self._speak_local(decision.speech, None)
        self._kick_deadline()

    def _speak_local(self, text: str, turn_id: str | None) -> None:
        if self.playback_queue is None:
            return
        self.playback_queue.enqueue(
            PlaybackItem(Source.GEMMA, self._tts.stream(text), turn_id=turn_id)
        )

    def _on_turn_state(self, key: int, state: TurnState) -> None:
        self.bus.emit("turn.state", {"turn_id": f"k-{key}", "state": state.value})
        if self.playback_queue is not None:
            self.playback_queue.set_gate(
                state in (TurnState.CAPTURING, TurnState.HOLDING)
            )
        self._kick_deadline()

    # ---- agent says become speech --------------------------------------------------

    def _wire_say_speech(self) -> None:
        def on_event(env) -> None:
            if env.type != "agent.say" or not env.payload.get("active"):
                return
            if self.playback_queue is None or self._tts is None:
                return
            self.playback_queue.enqueue(
                PlaybackItem(
                    Source.AGENT,
                    self._tts.stream(env.payload["text"]),
                    turn_id=env.payload.get("turn_id"),
                )
            )

        self.bus.subscribe(on_event)

    # ---- deadline pump ----------------------------------------------------------------

    def _kick_deadline(self) -> None:
        self._deadline_wakeup.set()

    async def _deadline_loop(self) -> None:
        while True:
            self._deadline_wakeup.clear()
            deadline = self.machine.next_deadline() if self.machine else None
            if deadline is None:
                await self._deadline_wakeup.wait()
                continue
            delay = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._deadline_wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self.machine.on_deadline()

    # ---- PTT ------------------------------------------------------------------------------

    def _wire_ptt(self) -> None:
        from voco.audio.ptt import PttHotkey

        key = self.cfg.get("audio", {}).get("ptt_key", "f9")
        try:
            self._ptt = PttHotkey(
                self.loop,
                on_press=self._on_ptt_press,
                on_release=self._on_ptt_release,
                key=key,
            )
            self._ptt.start()
        except Exception as e:
            self.bus.emit("daemon.error", {"error": f"ptt unavailable: {e}"})

    def _on_ptt_press(self) -> None:
        if self.playback_queue is not None:
            self.playback_queue.barge_in()
        self.machine.ptt_pressed()

    def _on_ptt_release(self) -> None:
        self.machine.ptt_released()
        self._kick_deadline()

    # ---- run ------------------------------------------------------------------------------

    async def run(self, host: str = "127.0.0.1", port: int = 7777) -> None:
        self.loop = asyncio.get_running_loop()
        self._wire_say_speech()
        runner = await run_server(self.bridge, host=host, port=port)
        print(f"voco-d listening on {host}:{port}" + (" (no audio)" if self.no_audio else ""))
        if not self.no_audio:
            self._wire_audio()
            self._player.bind_loop(self.loop)
            await self._bank.ensure()
            self._mic.start()
            self._wire_ptt()
            self.loop.create_task(self._deadline_loop())
        try:
            await asyncio.Event().wait()
        finally:
            await self.bridge.shutdown()
            await runner.cleanup()
            if self._mic is not None:
                self._mic.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco-d")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    daemon = Daemon(cfg, no_audio=args.no_audio)
    try:
        asyncio.run(daemon.run(port=args.port))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
