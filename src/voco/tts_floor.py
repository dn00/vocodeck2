"""voco-tts-floor — the bundled portability-floor TTS server (SPEC §4.3).

ROLE: a standalone OpenAI-compatible /v1/audio/speech endpoint wrapping
kokoro-onnx (CPU, any OS), so voco works on machines with no mlx-audio or
faster-qwen3-tts. It is a peer service, not part of the daemon — the
daemon only ever speaks the OpenAI interface.

INVARIANTS: streams int16 PCM at 24kHz, sentence-chunked when the engine
supports streaming; model files auto-download to models/ on first run;
loopback bind only, same as everything else.
"""

from __future__ import annotations

import argparse
import asyncio
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from aiohttp import web

RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
MODEL_FILES = {
    "kokoro-v1.0.onnx": f"{RELEASE}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{RELEASE}/voices-v1.0.bin",
}
SAMPLE_RATE = 24_000


def ensure_models(model_dir: Path) -> tuple[Path, Path]:
    model_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, url in MODEL_FILES.items():
        path = model_dir / name
        if not path.exists():
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(url, path)
        paths.append(path)
    return paths[0], paths[1]


class FloorTts:
    def __init__(self, model_dir: Path) -> None:
        from kokoro_onnx import Kokoro

        model, voices = ensure_models(model_dir)
        self._kokoro = Kokoro(str(model), str(voices))

    async def synth_stream(self, text: str, voice: str, speed: float):
        """Yield int16 PCM chunks; per-sentence when the engine streams."""
        create_stream = getattr(self._kokoro, "create_stream", None)
        if create_stream is not None:
            async for samples, sr in create_stream(text, voice=voice, speed=speed):
                yield self._to_pcm(samples, sr)
            return
        loop = asyncio.get_running_loop()
        samples, sr = await loop.run_in_executor(
            None, lambda: self._kokoro.create(text, voice=voice, speed=speed)
        )
        yield self._to_pcm(samples, sr)

    @staticmethod
    def _to_pcm(samples: Any, sr: int) -> bytes:
        data = np.asarray(samples)
        if data.dtype != np.int16:
            data = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        if sr != SAMPLE_RATE:  # kokoro is natively 24kHz; guard anyway
            idx = np.linspace(0, len(data) - 1, int(len(data) * SAMPLE_RATE / sr))
            data = data[idx.astype(np.int64)]
        return data.tobytes()


def build_app(tts: FloorTts) -> web.Application:
    async def speech(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        text = str(body.get("input", "")).strip()
        if not text:
            raise web.HTTPBadRequest(text="input required")
        voice = str(body.get("voice", "af_heart"))
        speed = float(body.get("speed", 1.0))
        resp = web.StreamResponse()
        resp.content_type = "application/octet-stream"
        await resp.prepare(request)
        async for chunk in tts.synth_stream(text, voice, speed):
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/audio/speech", speech)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="voco-tts-floor")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    args = parser.parse_args()
    tts = FloorTts(args.model_dir)
    print(f"voco-tts-floor on 127.0.0.1:{args.port} (kokoro-onnx, cpu)")
    web.run_app(build_app(tts), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
