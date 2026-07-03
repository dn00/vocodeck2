"""providers-smoke — stand up and measure every provider BEFORE trusting the
latency ladder (SPEC §5.1 exit criterion; BUILD.md pre-mortem item 1).

Usage:  uv run python scripts/providers_smoke.py [--config configs/mac-m1.toml]

Checks, each independent and skippable:
  1. silero VAD onnx model (downloads to models/ if missing), per-frame cost
  2. STT provider: transcribe 1s of synthetic audio, wall time
  3. TTS endpoint: /v1/audio/speech streaming — TTFA (time to first audio
     chunk) and total for a one-sentence utterance
  4. LLM endpoint (if configured): one-token completion round trip
  5. audio devices: list input/output defaults (sounddevice)

Prints a table; exits 0 even on failures (it's a diagnostic, not a gate).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

SILERO_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/"
    "src/silero_vad/data/silero_vad.onnx"
)

RESULTS: list[tuple[str, str]] = []


def report(name: str, outcome: str) -> None:
    RESULTS.append((name, outcome))
    print(f"  {name:<28} {outcome}")


def check_vad(model_path: Path) -> None:
    import numpy as np

    if not model_path.exists():
        model_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading silero to {model_path} ...")
        urllib.request.urlretrieve(SILERO_URL, model_path)
    from voco.adapters.silero import load_silero
    from voco.core.vad import FRAME_SAMPLES

    model = load_silero(str(model_path))
    frame = (np.random.randn(FRAME_SAMPLES) * 1000).astype(np.int16)
    model(frame)  # warm
    t0 = time.perf_counter()
    for _ in range(100):
        model(frame)
    per_frame_ms = (time.perf_counter() - t0) * 10
    report("vad (silero onnx)", f"OK  {per_frame_ms:.2f}ms/frame (budget: <32ms)")


def check_stt(cfg: dict) -> None:
    import numpy as np

    stt_cfg = dict(cfg.get("stt", {"provider": "faster-whisper"}))
    provider = stt_cfg.pop("provider")
    from voco.adapters.stt import build_stt

    t0 = time.perf_counter()
    stt = build_stt(provider, **stt_cfg)
    load_s = time.perf_counter() - t0
    tone = (np.sin(np.linspace(0, 440 * 2 * np.pi, 16000)) * 8000).astype(np.int16)
    t0 = time.perf_counter()
    stt.transcribe(tone.tobytes())
    wall = time.perf_counter() - t0
    report(
        f"stt ({provider})", f"OK  load {load_s:.1f}s, 1s-audio in {wall * 1000:.0f}ms"
    )


async def check_tts(cfg: dict) -> None:
    tts_cfg = cfg.get(
        "tts",
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "kokoro",
            "voice": "af_heart",
        },
    )
    from voco.adapters.tts import OpenAICompatibleTts

    tts = OpenAICompatibleTts(
        base_url=tts_cfg["base_url"],
        model=tts_cfg["model"],
        voice=tts_cfg["voice"],
        sample_rate=int(tts_cfg.get("sample_rate", 24_000)),
        api_key=tts_cfg.get("api_key"),
    )
    t0 = time.perf_counter()
    first = None
    total_bytes = 0
    async for chunk in tts.stream("Voco is online and ready."):
        if first is None:
            first = time.perf_counter() - t0
        total_bytes += len(chunk)
    total = time.perf_counter() - t0
    secs = total_bytes / (2 * tts.sample_rate)
    report(
        f"tts ({tts_cfg['base_url']})",
        f"OK  TTFA {first * 1000:.0f}ms, {secs:.1f}s audio in {total * 1000:.0f}ms",
    )


async def check_llm(cfg: dict) -> None:
    llm_cfg = cfg.get("first_mate")
    if not llm_cfg:
        report("first mate (llm)", "SKIP (not configured)")
        return
    import aiohttp

    t0 = time.perf_counter()
    async with (
        aiohttp.ClientSession() as s,
        s.post(
            f"{llm_cfg['base_url'].rstrip('/')}/chat/completions",
            json={
                "model": llm_cfg.get("model", ""),
                "messages": [{"role": "user", "content": "Say OK."}],
                "max_tokens": 4,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp,
    ):
        resp.raise_for_status()
        await resp.json()
    report(
        "first mate (llm)", f"OK  round trip {(time.perf_counter() - t0) * 1000:.0f}ms"
    )


def check_devices() -> None:
    import sounddevice as sd

    d_in, d_out = sd.default.device
    devs = sd.query_devices()
    in_name = devs[d_in]["name"] if d_in is not None and d_in >= 0 else "none"
    out_name = devs[d_out]["name"] if d_out is not None and d_out >= 0 else "none"
    report("audio devices", f"OK  in={in_name!r} out={out_name!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    cfg = tomllib.loads(args.config.read_text()) if args.config else {}
    model_path = Path(
        cfg.get("audio", {}).get("silero_model", "models/silero_vad.onnx")
    )

    print("voco providers smoke:")
    for name, fn in [
        ("vad", lambda: check_vad(model_path)),
        ("stt", lambda: check_stt(cfg)),
        ("tts", lambda: asyncio.run(check_tts(cfg))),
        ("llm", lambda: asyncio.run(check_llm(cfg))),
        ("devices", check_devices),
    ]:
        try:
            fn()
        except Exception as e:
            report(name, f"FAIL  {type(e).__name__}: {e}")
    failures = sum(1 for _, o in RESULTS if o.startswith("FAIL"))
    print(f"\n{len(RESULTS) - failures}/{len(RESULTS)} checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
