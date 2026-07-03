"""Machine-validated audio round trip — no ears required (BUILD.md).

Boots the floor TTS in-process, synthesizes a spoken command, then runs
the produced audio through the REAL production components: silero VAD
(hysteresis gate) and faster-whisper STT. Passes when the VAD detects one
utterance and the transcript matches the input closely enough to route.

Run: uv run python scripts/roundtrip_smoke.py
(deps: uv sync --extra stt --extra floor; downloads ~800MB of models on
first run)
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
from pathlib import Path

import numpy as np

PHRASES = [
    "run the test suite",
    "switch to Helena",
    "what sessions are connected",
]


async def main() -> None:
    from voco.adapters.silero import load_silero
    from voco.adapters.stt import FasterWhisperStt
    from voco.core.vad import FRAME_SAMPLES, VadConfig, VadGate
    from voco.tts_floor import SAMPLE_RATE, FloorTts

    print("loading models (first run downloads)...")
    t0 = time.perf_counter()
    tts = FloorTts(Path("models"))
    stt = FasterWhisperStt(model_size="small", device="cpu")
    vad_model = load_silero("models/silero_vad.onnx")
    print(f"  loaded in {time.perf_counter() - t0:.1f}s\n")

    failures = 0
    for phrase in PHRASES:
        t0 = time.perf_counter()
        pcm24 = bytearray()
        first = None
        async for chunk in tts.synth_stream(phrase, "af_heart", 1.0):
            if first is None:
                first = time.perf_counter() - t0
            pcm24.extend(chunk)
        synth_s = time.perf_counter() - t0

        # Resample 24kHz -> 16kHz for the mic-side components.
        audio24 = np.frombuffer(bytes(pcm24), dtype=np.int16)
        idx = np.linspace(0, len(audio24) - 1, int(len(audio24) * 16 / 24))
        audio16 = audio24[idx.astype(np.int64)]

        # Real VAD hysteresis over the real audio (+ trailing silence).
        events: list[str] = []
        gate = VadGate(
            VadConfig(),
            model=vad_model,
            on_speech_started=lambda log=events: log.append("start"),
            on_speech_ended=lambda log=events: log.append("end"),
        )
        padded = np.concatenate(
            [audio16, np.zeros(16_000, dtype=np.int16)]  # 1s silence tail
        )
        for i in range(0, len(padded) - FRAME_SAMPLES, FRAME_SAMPLES):
            gate.process(padded[i : i + FRAME_SAMPLES])

        t0 = time.perf_counter()
        transcript = stt.transcribe(audio16.tobytes())
        stt_s = time.perf_counter() - t0

        norm = lambda s: re.sub(r"[^a-z ]", "", s.lower()).strip()  # noqa: E731
        similarity = difflib.SequenceMatcher(
            None, norm(phrase), norm(transcript)
        ).ratio()
        ok = (
            events.count("start") == 1 and events.count("end") == 1 and similarity > 0.8
        )
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {phrase!r}")
        print(
            f"     tts ttfa {first * 1000:.0f}ms, total {synth_s * 1000:.0f}ms"
            f" ({len(audio24) / SAMPLE_RATE:.1f}s audio)"
        )
        print(f"     vad events {events} | stt {stt_s * 1000:.0f}ms -> {transcript!r}")
        print(f"     similarity {similarity:.2f}\n")

    print(f"{len(PHRASES) - failures}/{len(PHRASES)} round trips passed")


if __name__ == "__main__":
    asyncio.run(main())
