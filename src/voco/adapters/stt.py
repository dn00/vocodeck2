"""STT port + providers (SPEC §4.2).

ROLE: transcribe one utterance of 16kHz int16 PCM to text. Providers are
lazy-imported so the daemon (and tests) run without heavy deps installed.
M0 ships faster-whisper (portable) and Null (tests/smoke); parakeet and the
mlx variants follow per the platform matrix.

INVARIANTS: transcribe() is called off the event loop's critical path (the
shell runs it in a thread executor); empty audio returns "".
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class SttPort(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


class NullStt:
    """Echoes a fixed string; for tests and --no-audio bring-up."""

    def __init__(self, canned: str = "") -> None:
        self.canned = canned

    def transcribe(self, pcm: bytes) -> str:
        return self.canned


class FasterWhisperStt:
    def __init__(self, model_size: str = "small", device: str = "auto") -> None:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        compute = "int8" if device in ("cpu", "auto") else "float16"
        self._model = WhisperModel(model_size, device=device, compute_type=compute)

    def transcribe(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(audio, language=None, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()


def build_stt(provider: str, **kwargs) -> SttPort:
    if provider == "faster-whisper":
        return FasterWhisperStt(**kwargs)
    if provider == "null":
        return NullStt(**kwargs)
    raise ValueError(f"unknown stt provider: {provider} (M0 ships faster-whisper)")
