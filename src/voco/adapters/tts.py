"""TTS provider + cached phrase bank (SPEC §4.3).

ROLE: the one TTS interface — any OpenAI-compatible /v1/audio/speech
endpoint streaming raw PCM — plus the disk-cached phrase bank that makes
acks playable in ≤120ms with no model in the loop.

INVARIANTS: streaming chunks are yielded as they arrive (TTFA is the
provider's, not ours); phrase bank keys are (voice, phrase-hash) under the
provider's cache dir so a voice change re-synthesizes; synthesis failures
raise to the caller (the daemon routes them to daemon.error — never
swallowed silently here).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp


class OpenAICompatibleTts:
    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        sample_rate: int = 24_000,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.sample_rate = sample_rate
        self._api_key = api_key

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.base_url}/audio/speech",
                json={
                    "model": self.model,
                    "voice": self.voice,
                    "input": text,
                    "response_format": "pcm",
                    "stream": True,
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120, sock_connect=5),
            ) as resp,
        ):
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(4096):
                yield chunk

    async def synth_bytes(self, text: str) -> bytes:
        out = bytearray()
        async for chunk in self.stream(text):
            out.extend(chunk)
        return bytes(out)


ACK_PHRASES = ["On it.", "Sending that over.", "One sec.", "Done listening."]
EARCON_NAMES = ["chirp", "line-dead", "chime"]


def _tone(freqs: list[float], ms: int, sample_rate: int) -> bytes:
    """Synthesized earcons — no TTS dependency for the non-verbal sounds."""
    import numpy as np

    t = np.linspace(0, ms / 1000, int(sample_rate * ms / 1000), endpoint=False)
    wave = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    envelope = np.minimum(1.0, np.minimum(t / 0.01, (ms / 1000 - t) / 0.05))
    return (wave * envelope * 12_000).astype("int16").tobytes()


def make_earcon(name: str, sample_rate: int) -> bytes:
    if name == "chirp":
        return _tone([880.0], 90, sample_rate) + _tone([1320.0], 90, sample_rate)
    if name == "line-dead":
        return _tone([440.0, 466.0], 250, sample_rate)
    if name == "chime":
        return _tone([660.0], 120, sample_rate)
    raise ValueError(name)


class PhraseBank:
    def __init__(self, tts: OpenAICompatibleTts, cache_dir: Path) -> None:
        self._tts = tts
        self._dir = cache_dir
        self._mem: dict[str, bytes] = {}

    def _key(self, phrase: str) -> str:
        h = hashlib.sha256(f"{self._tts.voice}:{phrase}".encode()).hexdigest()[:16]
        return f"{h}.pcm"

    async def ensure(self) -> list[str]:
        """Fill the bank; returns phrases that failed to synthesize.

        Earcons are synthesized locally and never fail; a down TTS server
        must not prevent the voice loop from starting (the caller reports
        failures on the event bus).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        for name in EARCON_NAMES:
            self._mem[name] = make_earcon(name, self._tts.sample_rate)
        failed: list[str] = []
        for phrase in ACK_PHRASES:
            path = self._dir / self._key(phrase)
            if path.exists():
                self._mem[phrase] = path.read_bytes()
                continue
            try:
                pcm = await self._tts.synth_bytes(phrase)
            except Exception:
                failed.append(phrase)
                continue
            path.write_bytes(pcm)
            self._mem[phrase] = pcm
        return failed

    def get(self, key: str) -> bytes | None:
        return self._mem.get(key)

    def duration_ms(self, key: str) -> int:
        pcm = self._mem.get(key, b"")
        return len(pcm) * 1000 // (2 * self._tts.sample_rate)
