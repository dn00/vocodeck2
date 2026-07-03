"""Acoustic echo cancellation — partitioned-block FDAF (SPEC §4.4 AEC, v2
pulled forward).

ROLE: subtract our own playback from the mic signal so full-duplex works
on open speakers. Pure numpy (no native deps — the webrtc/speex bindings
don't build on arm64), frequency-domain block NLMS with 8 partitions
(≈256ms echo tail, absorbing typical device latency without explicit
delay estimation).

INVARIANTS: frame-locked to the pipeline (512 samples @16kHz); with no
recent playback the canceller is a passthrough and never adapts (near-end
speech is untouched); adaptation is energy-normalized with a divergence
guard. Known limitation (documented in BUILD.md): no double-talk detector
yet — heavy simultaneous speech can partially suppress the near end;
half_duplex remains the fallback mode.
"""

from __future__ import annotations

import collections

import numpy as np

FRAME = 512  # samples @16kHz — matches core.vad
PARTITIONS = 8  # 8 × 32ms = 256ms echo tail
FFT = 2 * FRAME
BINS = FFT // 2 + 1
MU = 0.4  # NLMS step size
EPS = 1e-8
REF_SILENCE_RMS = 30.0  # below this, playback is considered silent


class EchoCanceller:
    def __init__(self) -> None:
        self._weights = np.zeros((PARTITIONS, BINS), dtype=np.complex128)
        self._ref_spectra = np.zeros((PARTITIONS, BINS), dtype=np.complex128)
        self._prev_ref = np.zeros(FRAME, dtype=np.float64)
        self._ref_queue: collections.deque[np.ndarray] = collections.deque(maxlen=64)

    def push_playback(self, frame: np.ndarray) -> None:
        """Reference signal: 512-sample 16kHz frames, as played. Thread-safe
        enough under the GIL (deque append vs popleft)."""
        self._ref_queue.append(frame.astype(np.float64))

    def process(self, mic: np.ndarray) -> np.ndarray:
        """Return mic with the estimated echo removed (int16 in/out)."""
        mic_f = mic.astype(np.float64)
        ref = (
            self._ref_queue.popleft()
            if self._ref_queue
            else np.zeros(FRAME, dtype=np.float64)
        )
        ref_active = np.sqrt(np.mean(ref**2)) > REF_SILENCE_RMS

        # Overlap-save: spectrum of [previous ref | current ref].
        x = np.concatenate([self._prev_ref, ref])
        self._prev_ref = ref
        self._ref_spectra = np.roll(self._ref_spectra, 1, axis=0)
        self._ref_spectra[0] = np.fft.rfft(x)

        if not ref_active and not self._tail_active():
            return mic  # passthrough: nothing of ours to cancel

        echo_spec = np.sum(self._weights * self._ref_spectra, axis=0)
        echo = np.fft.irfft(echo_spec, FFT)[FRAME:]
        error = mic_f - echo

        # Frequency-domain NLMS update on the error block.
        err_spec = np.fft.rfft(np.concatenate([np.zeros(FRAME), error]))
        norm = np.sum(np.abs(self._ref_spectra) ** 2, axis=0) + EPS
        self._weights += MU * np.conj(self._ref_spectra) * err_spec / norm
        # Gradient constraint: keep each partition causal (zero the tail
        # half in time domain) — prevents circular-convolution artifacts.
        w_time = np.fft.irfft(self._weights, FFT, axis=1)
        w_time[:, FRAME:] = 0.0
        self._weights = np.fft.rfft(w_time, FFT, axis=1)

        # Divergence guard: cancelling must not ADD energy.
        if np.sum(error**2) > 2.0 * np.sum(mic_f**2):
            self._weights *= 0.5
            error = mic_f

        return np.clip(error, -32768, 32767).astype(np.int16)

    def _tail_active(self) -> bool:
        """Echo can outlive playback by the filter tail length."""
        return bool(np.any(np.abs(self._ref_spectra) > REF_SILENCE_RMS * np.sqrt(FFT)))


def resample_to_16k(pcm: bytes, src_rate: int) -> np.ndarray:
    """Playback tap helper: raw int16 bytes at src_rate → 16kHz samples."""
    data = np.frombuffer(pcm, dtype=np.int16)
    if src_rate == 16_000 or len(data) == 0:
        return data.copy()
    n_out = int(len(data) * 16_000 / src_rate)
    idx = np.linspace(0, len(data) - 1, n_out)
    return data[idx.astype(np.int64)]
