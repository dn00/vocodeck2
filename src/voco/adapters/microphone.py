"""Microphone input stream (SPEC §4.1) — sounddevice adapter.

ROLE: hardware edge only; frames go to the injected callback (the daemon
marshals them onto the event loop and into core.capture/core.vad).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from voco.core.vad import FRAME_SAMPLES, SAMPLE_RATE


class MicStream:
    """sounddevice input stream with bounded automatic device recovery."""

    def __init__(
        self,
        on_frame: Callable[[np.ndarray], None],
        device: int | str | None = None,
        on_error: Callable[[str], None] | None = None,
        monitor_interval_s: float = 1.0,
        retry_initial_s: float = 1.0,
    ) -> None:
        self._on_frame = on_frame
        self._on_error = on_error or (lambda message: None)
        self._device = device
        self._stream: Any = None
        self._monitor_interval_s = monitor_interval_s
        self._retry_initial_s = retry_initial_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_status = ""
        self._last_status_ts = 0.0

    def _report(self, message: str) -> None:
        try:
            self._on_error(message)
        except Exception:
            pass  # a diagnostics callback must never kill PortAudio

    def _open(self) -> bool:
        import sounddevice as sd

        def callback(indata, frames, time_info, status) -> None:
            if status:
                message = str(status)
                now = time.monotonic()
                if message != self._last_status or now - self._last_status_ts >= 5.0:
                    self._last_status = message
                    self._last_status_ts = now
                    self._report(f"microphone stream status: {message}")
            try:
                self._on_frame(indata[:, 0].copy())
            except Exception as e:
                self._report(f"microphone frame callback failed: {e}")

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=callback,
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close()
            except Exception:
                pass
            raise
        with self._lock:
            if self._stop.is_set():
                keep = False
            else:
                self._stream = stream
                keep = True
        if not keep:
            # stop() may race a slow device open. Never publish or leak the
            # stream after shutdown has begun.
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        return keep

    def start(self) -> None:
        self._stop.clear()
        self._open()  # initial failure is fatal to voice startup and named there
        self._thread = threading.Thread(
            target=self._monitor, name="voco-mic-monitor", daemon=True
        )
        self._thread.start()

    def _close_stream(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _monitor(self) -> None:
        while not self._stop.wait(self._monitor_interval_s):
            with self._lock:
                stream = self._stream
            try:
                active = bool(stream is not None and stream.active)
            except Exception:
                active = False
            if active:
                continue
            self._report("microphone stream stopped; reconnecting")
            self._close_stream()
            delay = self._retry_initial_s
            while not self._stop.is_set():
                try:
                    restored = self._open()
                except Exception as e:
                    self._report(f"microphone reconnect failed: {e}")
                    if self._stop.wait(delay):
                        return
                    delay = min(delay * 2.0, 30.0)
                else:
                    if not restored:
                        return
                    self._report("microphone stream restored")
                    break

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close_stream()
