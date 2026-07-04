"""The audio-side shell: mic → VAD → turn machine → STT → route → speech.

ROLE: compose the voice pipeline (SPEC §4–§5) around the pure core. Owns
every audio member non-optionally — the daemon holds `VoiceLoop | None`
and this module never half-exists. Decisions and dispatch stay in the
daemon, reached through the VoiceHost port.

INVARIANTS: PortAudio callbacks are marshaled onto the asyncio loop before
touching core state; speculative STT tasks are revision-keyed and
cancelled on merge (SPEC §5.2); the rule-0 gate follows every turn-state
change; deadline waits use the same monotonic clock as the machine.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from voco.adapters.hotkey import PttHotkey
from voco.adapters.microphone import MicStream
from voco.adapters.silero import load_silero
from voco.adapters.speaker import SpeakerPlayer
from voco.adapters.stt import build_stt
from voco.adapters.tts import OpenAICompatibleTts, PhraseBank
from voco.core.arbitration import DuplexMode, PlaybackItem, PlaybackQueue, Source
from voco.core.attention import AttentionGate, AttentionMode
from voco.core.capture import CaptureBuffer, pre_roll_frames_for
from voco.core.echo import FRAME, EchoCanceller, resample_to_16k
from voco.core.events import EventBus
from voco.core.phrases import PhraseCommand
from voco.core.router import Routed
from voco.core.turn import (
    RouteDecision,
    TurnConfig,
    TurnEvents,
    TurnMachine,
    TurnState,
)
from voco.core.vad import VadConfig, VadGate


class VoiceHost(Protocol):
    """What the daemon provides to the voice loop (decisions + dispatch)."""

    async def route(self, text: str) -> Routed: ...

    def run_phrase(self, cmd: PhraseCommand) -> None: ...

    def dispatch(self, text: str, decision: RouteDecision) -> tuple[str, str]:
        """Dispatch to a session; returns (turn_id, DispatchResult)."""
        ...


@dataclass
class VoiceLoopDeps:
    """Impure edges, injected with production defaults (house standard)."""

    load_vad_model: Callable[[str], Callable[[np.ndarray], float]] = load_silero
    stt_builder: Callable[..., Any] = build_stt
    tts_factory: Callable[..., Any] = OpenAICompatibleTts
    mic_factory: Callable[..., Any] = MicStream
    player_factory: Callable[..., Any] = SpeakerPlayer
    hotkey_factory: Callable[..., Any] | None = PttHotkey
    wake_loader: Callable[[str], Callable[[np.ndarray], float]] | None = None


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


class MateSpeechChannel:
    """Streams first-mate speech into ONE playback item while the JSON
    completion is still generating: text deltas are cut at sentence
    boundaries and synthesized per sentence, so the first clause is
    audible before the model has finished the rest of its output.

    INVARIANTS: nothing is enqueued until a full sentence exists (a
    cancelled channel that never completed a sentence leaves no trace);
    finish() flushes the remainder and reports whether anything streamed
    (the daemon blanks decision.speech to prevent double-speak);
    cancel() drops un-emitted text but never claws back queued audio."""

    def __init__(self, voice: VoiceLoop) -> None:
        self._voice = voice
        self._pending = ""
        self._sentences: asyncio.Queue[str | None] = asyncio.Queue()
        self._item: PlaybackItem | None = None
        self._turn_id: str | None = None
        self._closed = False
        self.consumed = False

    def set_turn_id(self, turn_id: str) -> None:
        """Dispatch happened: attribute the (possibly already playing)
        stream to its turn so arbitration rules 2/3 can police it."""
        self._turn_id = turn_id
        if self._item is not None:
            self._item.turn_id = turn_id

    def push(self, delta: str) -> None:
        if self._closed:
            return
        self._pending += delta
        parts = _SENTENCE_BOUNDARY.split(self._pending)
        for sentence in parts[:-1]:
            self._emit(sentence)
        self._pending = parts[-1]

    def finish(self) -> bool:
        """Stream completed normally: flush the tail, close the item."""
        if not self._closed:
            self._emit(self._pending)
            self._pending = ""
            self._close()
        return self.consumed

    def cancel(self) -> None:
        """Timeout/misroute: drop un-spoken text; queued audio finishes."""
        self._pending = ""
        self._close()

    def _close(self) -> None:
        self._closed = True
        if self._item is not None:
            self._sentences.put_nowait(None)

    def _emit(self, sentence: str) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        self.consumed = True
        if self._item is None:
            self._item = PlaybackItem(
                Source.FIRST_MATE, self._synth(), turn_id=self._turn_id
            )
            self._voice.queue.enqueue(self._item)
        self._sentences.put_nowait(sentence)

    async def _synth(self):
        while True:
            sentence = await self._sentences.get()
            if sentence is None:
                return
            async for chunk in self._voice.tts.stream(
                sentence, voice=self._voice.mate_voice
            ):
                yield chunk


class VoiceLoop:
    def __init__(
        self,
        cfg: dict[str, Any],
        bus: EventBus,
        host: VoiceHost,
        deps: VoiceLoopDeps | None = None,
    ) -> None:
        deps = deps or VoiceLoopDeps()
        self._deps = deps
        self._bus = bus
        self._host = host
        audio_cfg = cfg.get("audio", {})
        stt_cfg = dict(cfg.get("stt", {"provider": "faster-whisper"}))
        tts_cfg = cfg.get(
            "tts",
            {
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "kokoro",
                "voice": "af_heart",
            },
        )

        # The mate's own TTS voice (None = share the agent voice): the
        # user must be able to tell WHO is speaking (live-test ask).
        self.mate_voice: str | None = cfg.get("first_mate", {}).get("voice")
        self.duplex = DuplexMode(audio_cfg.get("duplex", DuplexMode.FULL.value))
        self.attention = AttentionGate(
            AttentionMode(audio_cfg.get("attention", AttentionMode.ALWAYS.value)),
            now=time.monotonic,
            wake_window_s=float(audio_cfg.get("wake_window_s", 30.0)),
        )
        self._premute = self.attention.mode
        self._ptt_key = str(audio_cfg.get("ptt_key", "f9"))

        self.tts = deps.tts_factory(
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
        self.bank = PhraseBank(self.tts, cache)
        self.stt = deps.stt_builder(stt_cfg.pop("provider"), **stt_cfg)

        self._aec = EchoCanceller() if audio_cfg.get("aec") else None
        self._aec_ref = bytearray()  # 16kHz reference accumulator
        self.queue: PlaybackQueue  # assigned below; the lambda closes over it
        self.player = deps.player_factory(
            on_finished=lambda: self.queue.on_item_finished(),
            on_playing_changed=self._on_playing_changed,
            sample_rate=self.tts.sample_rate,
            device=audio_cfg.get("output_device"),
            on_pcm_played=self._on_pcm_played if self._aec else None,
        )
        self.queue = PlaybackQueue(self.player, emit=bus.emit)
        self.queue.set_duplex(self.duplex)

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
                incomplete_hold_ms=int(audio_cfg.get("incomplete_hold_ms", 2000)),
            ),
            now=time.monotonic,
        )

        min_speech_ms = int(audio_cfg.get("min_speech_ms", 384))
        self.capture = CaptureBuffer(pre_roll_frames=pre_roll_frames_for(min_speech_ms))
        self.vad_gate = VadGate(
            VadConfig(
                threshold=float(audio_cfg.get("vad_threshold", 0.5)),
                min_speech_ms=min_speech_ms,
                min_speech_continuation_ms=int(
                    audio_cfg.get("min_speech_continuation_ms", 192)
                ),
                min_silence_ms=int(audio_cfg.get("min_silence_ms", 64)),
            ),
            model=deps.load_vad_model(
                str(audio_cfg.get("silero_model", "models/silero_vad.onnx"))
            ),
            on_speech_started=self._on_vad_speech_start,
            on_speech_ended=self.machine.speech_ended,
            reopenable=lambda: (
                self.machine.state in (TurnState.HOLDING, TurnState.REOPENABLE)
            ),
        )
        self.mic = deps.mic_factory(
            self._on_frame, device=audio_cfg.get("input_device")
        )
        self._wake_scorer: Callable[[np.ndarray], float] | None = None
        wake_model = audio_cfg.get("wake_model")
        if wake_model and deps.wake_loader is not None:
            self._wake_scorer = deps.wake_loader(str(wake_model))
        self._wake_threshold = float(audio_cfg.get("wake_threshold", 0.5))

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ptt: PttHotkey | None = None
        self._deadline_task: asyncio.Task[None] | None = None
        self._deadline_wakeup = asyncio.Event()
        self._speculation: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._playing = False  # mirrored from the player (loop thread)

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.player.bind_loop(loop)
        failed = await self.bank.ensure()
        if failed:
            self._bus.emit(
                "daemon.error",
                {
                    "error": f"phrase bank: {len(failed)} phrases"
                    " unsynthesized (TTS down?)"
                },
            )
        self.mic.start()
        try:
            if self._deps.hotkey_factory is None:
                raise RuntimeError("hotkey factory disabled")
            self._ptt = self._deps.hotkey_factory(
                loop,
                on_press=self._on_ptt_press,
                on_release=self._on_ptt_release,
                key=self._ptt_key,
            )
            self._ptt.start()
        except Exception as e:
            # Capability degrades (Wayland / missing pynput); loop stays up.
            self._bus.emit("daemon.error", {"error": f"ptt unavailable: {e}"})
        self._deadline_task = loop.create_task(self._deadline_loop())

    def stop(self) -> None:
        self.mic.stop()
        if self._ptt is not None:
            self._ptt.stop()
        if self._deadline_task is not None:
            self._deadline_task.cancel()

    # ---- daemon-facing controls ----------------------------------------------

    def set_duplex(self, mode: DuplexMode) -> None:
        self.duplex = mode
        self.queue.set_duplex(mode)
        # Mid-playback switches take effect NOW: flipping to half_duplex
        # while the bot is speaking is exactly the echo-rescue move
        # (live-test bug) — waiting for the next playback edge is too late.
        self.vad_gate.suppress(mode is DuplexMode.HALF and self._playing)

    def set_patience(
        self, hold_ms: int | None = None, incomplete_ms: int | None = None
    ) -> None:
        self.machine.set_patience(hold_ms=hold_ms, incomplete_ms=incomplete_ms)

    def set_attention(self, mode: AttentionMode) -> None:
        if mode is not AttentionMode.MUTED:
            self._premute = mode
        self.attention.set_mode(mode)

    def set_muted(self, muted: bool) -> None:
        """Phrase-table mute/unmute: MUTED <-> the last non-muted mode."""
        self.set_attention(AttentionMode.MUTED if muted else self._premute)

    def barge_in(self) -> None:
        self.queue.barge_in()

    def note_dispatch(self, turn_id: str) -> None:
        self.queue.note_dispatch(turn_id)

    def open_mate_speech_channel(self) -> MateSpeechChannel:
        return MateSpeechChannel(self)

    def _sentence_synth(self, text: str, voice: str | None):
        """Synthesize sentence-by-sentence inside ONE playback item: the
        first sentence is audible at ~one-sentence TTFA instead of
        scaling with message length (triage: sentence-chunked TTS)."""
        sentences = [
            s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()
        ] or [text]

        async def gen():
            for sentence in sentences:
                async for chunk in self.tts.stream(sentence, voice=voice):
                    yield chunk

        return gen()

    def speak_local(self, text: str, turn_id: str | None) -> None:
        self.queue.enqueue(
            PlaybackItem(
                Source.FIRST_MATE,
                self._sentence_synth(text, self.mate_voice),
                turn_id=turn_id,
            )
        )

    def speak_agent(self, text: str, turn_id: str | None) -> None:
        self.queue.enqueue(
            PlaybackItem(
                Source.AGENT, self._sentence_synth(text, None), turn_id=turn_id
            )
        )

    def play_bank(self, key: str) -> None:
        pcm = self.bank.get(key)
        if pcm:
            self.queue.enqueue(
                PlaybackItem(Source.ACK, pcm, duration_ms=self.bank.duration_ms(key))
            )

    def dispatch_feedback(self, turn_id: str, result: str) -> None:
        """Shared post-dispatch audio policy (voice and typed paths)."""
        self.note_dispatch(turn_id)
        if result in ("no_session", "queued_idle"):
            self.play_bank("line-dead")

    # ---- audio-thread edges ------------------------------------------------------

    def _on_frame(self, frame: np.ndarray) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._process_frame, frame)

    def _on_pcm_played(self, pcm: bytes) -> None:
        # PortAudio output thread: resample + frame the AEC reference.
        if self._aec is None:  # tap is only wired when AEC is on; guard anyway
            return
        samples = resample_to_16k(pcm, self.tts.sample_rate)
        self._aec_ref.extend(samples.tobytes())
        frame_bytes = FRAME * 2
        while len(self._aec_ref) >= frame_bytes:
            chunk = bytes(self._aec_ref[:frame_bytes])
            del self._aec_ref[:frame_bytes]
            self._aec.push_playback(np.frombuffer(chunk, dtype=np.int16))

    def _process_frame(self, frame: np.ndarray) -> None:
        if self._aec is not None:
            frame = self._aec.process(frame)
        self.capture.feed(frame)
        if (
            self._wake_scorer is not None
            and self.attention.mode is AttentionMode.WAKE
            and not self.attention.allows_vad()
            # Deaf-while-speaking covers the wake ear too: TTS audio must
            # not wake the deck it came from.
            and not self.vad_gate.suppressed
            and self._wake_scorer(frame) >= self._wake_threshold
        ):
            self.attention.on_wake_word()
            self.play_bank("chime")  # audible: the deck is listening
        self.vad_gate.process(frame)

    def _on_playing_changed(self, playing: bool) -> None:
        # Half-duplex: deaf while we speak (+ grace via VAD run reset).
        self._playing = playing
        if self.duplex is DuplexMode.HALF:
            self.vad_gate.suppress(playing)
            if not playing:
                # The ring buffered our own speaker tail while suppressed;
                # the next utterance must not open with bot audio.
                self.capture.drop_pre_roll()

    def _on_vad_speech_start(self) -> None:
        if not self.attention.allows_vad():
            return
        if self.duplex is DuplexMode.FULL:
            self.queue.barge_in()  # rule 1
        self.machine.speech_started()

    def _on_ptt_press(self) -> None:
        if not self.attention.allows_ptt():
            return
        self.queue.barge_in()
        self.machine.ptt_pressed()

    def _on_ptt_release(self) -> None:
        self.machine.ptt_released()
        self._kick_deadline()

    # ---- turn machine listeners -----------------------------------------------------

    def _on_capture_started(self, key: int, reopened: bool) -> None:
        if reopened:
            self.capture.resume_utterance()
        else:
            self.capture.clear()
            self.capture.start_utterance()
        self._kick_deadline()

    def _on_capture_stopped(self, key: int, revision: int) -> None:
        self.capture.pause()
        pcm = self.capture.take()
        assert self._loop is not None
        task = self._loop.create_task(self._transcribe(key, revision, pcm))
        self._speculation[(key, revision)] = task
        self._kick_deadline()

    async def _transcribe(self, key: int, revision: int, pcm: bytes) -> None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, self.stt.transcribe, pcm)
        except Exception as e:
            self._bus.emit("daemon.error", {"error": f"stt: {e}"})
            text = ""
        self._speculation.pop((key, revision), None)
        self._bus.emit("stt.final", {"text": text})
        self.machine.stt_final(key, revision, text)
        self._kick_deadline()

    def _on_chirp(self, key: int) -> None:
        self.play_bank("chirp")

    def _on_cancel(self, key: int, revision: int) -> None:
        task = self._speculation.pop((key, revision), None)
        if task is not None:
            task.cancel()
        # Cancel speculative local speech; agent speech is never speculative
        # so flushing local sources is safe here (SPEC §5.2).
        self.queue.barge_in()

    def _on_route_requested(self, key: int, revision: int, text: str) -> None:
        assert self._loop is not None
        self._loop.create_task(self._route_turn(key, revision, text))

    async def _route_turn(self, key: int, revision: int, text: str) -> None:
        routed = await self._host.route(text)
        if routed.phrase is not None:
            self._host.run_phrase(routed.phrase)
            # Phrase commands never dispatch: close as a speechless answer.
            self.machine.route_decided(
                key, revision, RouteDecision(kind="answer", speech="")
            )
            return
        self.machine.route_decided(
            key, revision, routed.decision or RouteDecision(kind="forward")
        )
        self._kick_deadline()

    def _on_dispatch_ready(self, key: int, text: str, decision: RouteDecision) -> None:
        self.attention.on_turn_activity()
        turn_id, result = self._host.dispatch(text, decision)
        self.dispatch_feedback(turn_id, result)
        if decision.kind == "ack_forward" and decision.speech:
            self.speak_local(decision.speech, turn_id)

    def _on_local_reply(self, key: int, decision: RouteDecision) -> None:
        self.attention.on_turn_activity()
        if decision.speech:
            self.speak_local(decision.speech, None)
        self._kick_deadline()

    def _on_turn_state(self, key: int, state: TurnState) -> None:
        self._bus.emit("turn.state", {"turn_id": f"k-{key}", "state": state.value})
        self.queue.set_gate(state in (TurnState.CAPTURING, TurnState.HOLDING))
        self._kick_deadline()

    # ---- deadline pump -----------------------------------------------------

    def _kick_deadline(self) -> None:
        self._deadline_wakeup.set()

    async def _deadline_loop(self) -> None:
        while True:
            self._deadline_wakeup.clear()
            deadline = self.machine.next_deadline()
            if deadline is None:
                await self._deadline_wakeup.wait()
                continue
            delay = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._deadline_wakeup.wait(), timeout=delay)
            except TimeoutError:
                self.machine.on_deadline()
