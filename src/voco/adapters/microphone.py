"""Microphone input stream (SPEC §4.1) — sounddevice adapter.

ROLE: hardware edge only; frames go to the injected callback (the daemon
marshals them onto the event loop and into core.capture/core.vad).
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from voco.core.vad import FRAME_SAMPLES, SAMPLE_RATE


class MicStream:
    """sounddevice input stream → frame callback. Hardware edge, no logic."""

    def __init__(
        self,
        on_frame: Callable[[np.ndarray], None],
        device: int | str | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._device = device
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd  # noqa: PLC0415  (hardware edge, lazy)

        def callback(indata, frames, time_info, status) -> None:
            self._on_frame(indata[:, 0].copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
