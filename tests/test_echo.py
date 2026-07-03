"""Echo canceller machine validation (core/echo.py) — synthetic room."""

from __future__ import annotations

import numpy as np

from voco.core.echo import FRAME, EchoCanceller, resample_to_16k


def synth_far(seconds: float, seed: int = 7) -> np.ndarray:
    """Speech-like reference: band-limited noise with syllabic envelope."""
    rng = np.random.default_rng(seed)
    n = int(16_000 * seconds)
    noise = rng.standard_normal(n)
    # crude band-limit via moving average + syllable-rate AM
    kernel = np.ones(8) / 8
    band = np.convolve(noise, kernel, mode="same")
    t = np.arange(n) / 16_000
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 3.1 * t)
    return (band * envelope * 6000).astype(np.int16)


def through_room(far: np.ndarray) -> np.ndarray:
    """Echo path: 40ms bulk delay + a few decaying reflections."""
    h = np.zeros(1600)
    h[640] = 0.7
    h[800] = 0.25
    h[1100] = 0.12
    echo = np.convolve(far.astype(np.float64), h)[: len(far)]
    return echo


def run(canceller: EchoCanceller, far: np.ndarray, mic: np.ndarray) -> np.ndarray:
    out = np.zeros_like(mic)
    for i in range(0, len(mic) - FRAME, FRAME):
        canceller.push_playback(far[i : i + FRAME])
        out[i : i + FRAME] = canceller.process(mic[i : i + FRAME])
    return out


def erle_db(mic: np.ndarray, out: np.ndarray) -> float:
    e_in = np.sum(mic.astype(np.float64) ** 2) + 1e-9
    e_out = np.sum(out.astype(np.float64) ** 2) + 1e-9
    return 10 * np.log10(e_in / e_out)


def test_converges_and_cancels_echo():
    far = synth_far(4.0)
    mic = through_room(far).astype(np.int16)  # echo only, near end silent
    out = run(EchoCanceller(), far, mic)
    # Judge the last second, after convergence.
    tail = slice(-16_000, None)
    erle = erle_db(mic[tail], out[tail])
    assert erle > 12.0, f"ERLE only {erle:.1f}dB"


def test_near_end_passthrough_when_no_playback():
    near = synth_far(1.0, seed=42)
    canceller = EchoCanceller()
    out = np.zeros_like(near)
    for i in range(0, len(near) - FRAME, FRAME):
        out[i : i + FRAME] = canceller.process(near[i : i + FRAME])
    # No reference → bit-exact passthrough.
    n = (len(near) // FRAME) * FRAME - FRAME
    assert np.array_equal(out[:n], near[:n])


def test_near_end_survives_after_convergence():
    far = synth_far(4.0)
    echo = through_room(far)
    near = np.zeros(len(far))
    near[-16_000:] = synth_far(1.0, seed=99).astype(np.float64)  # user talks
    mic = np.clip(echo + near, -32768, 32767).astype(np.int16)
    out = run(EchoCanceller(), far, mic)
    tail = slice(-16_000, None)
    kept = np.corrcoef(out[tail].astype(np.float64), near[tail])[0, 1]
    assert kept > 0.7, f"near-end correlation only {kept:.2f}"


def test_resample_tap():
    pcm24 = (np.arange(2400) % 100).astype(np.int16).tobytes()
    out = resample_to_16k(pcm24, 24_000)
    assert len(out) == 1600
