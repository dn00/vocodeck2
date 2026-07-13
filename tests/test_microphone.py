"""Microphone status reporting and automatic device recovery."""

from __future__ import annotations

import sys
import threading
import time

import numpy as np

from voco.adapters.microphone import MicStream


class FakeInputStream:
    def __init__(self, **kwargs) -> None:
        self.callback = kwargs["callback"]
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True


class FakeSoundDevice:
    def __init__(self) -> None:
        self.streams: list[FakeInputStream] = []

    def InputStream(self, **kwargs):
        stream = FakeInputStream(**kwargs)
        self.streams.append(stream)
        return stream


class BlockingStartStream(FakeInputStream):
    def __init__(self, release, **kwargs) -> None:
        super().__init__(**kwargs)
        self.release = release

    def start(self) -> None:
        self.release.wait(timeout=1)
        super().start()


def test_callback_reports_portaudio_status(monkeypatch):
    sd = FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    frames, errors = [], []
    mic = MicStream(frames.append, on_error=errors.append, monitor_interval_s=10)
    mic.start()
    try:
        sd.streams[0].callback(
            np.zeros((512, 1), dtype=np.int16), 512, None, "input overflow"
        )
        assert len(frames) == 1 and frames[0].shape == (512,)
        assert errors == ["microphone stream status: input overflow"]
    finally:
        mic.stop()


def test_inactive_stream_reopens_default_device(monkeypatch):
    sd = FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    errors = []
    mic = MicStream(
        lambda frame: None,
        on_error=errors.append,
        monitor_interval_s=0.01,
        retry_initial_s=0.01,
    )
    mic.start()
    try:
        sd.streams[0].active = False
        deadline = time.monotonic() + 1.0
        while len(sd.streams) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sd.streams) >= 2 and sd.streams[-1].active
        assert "microphone stream stopped; reconnecting" in errors
        assert "microphone stream restored" in errors
    finally:
        mic.stop()


def test_stop_during_reconnect_does_not_publish_or_leak_stream(monkeypatch):
    release = threading.Event()
    sd = FakeSoundDevice()

    def input_stream(**kwargs):
        if sd.streams:
            stream = BlockingStartStream(release, **kwargs)
        else:
            stream = FakeInputStream(**kwargs)
        sd.streams.append(stream)
        return stream

    sd.InputStream = input_stream
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    mic = MicStream(lambda frame: None, monitor_interval_s=0.01)
    mic.start()
    sd.streams[0].active = False
    deadline = time.monotonic() + 1.0
    while len(sd.streams) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(sd.streams) == 2
    stopped = threading.Thread(target=mic.stop)
    stopped.start()
    release.set()
    stopped.join(timeout=1)
    assert not stopped.is_alive()
    assert sd.streams[1].closed and not sd.streams[1].active
