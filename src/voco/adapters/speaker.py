"""Speaker playback (SPEC §4.3).

ROLE: the Player port implementation — plays PlaybackItems whose content is
either raw PCM bytes (cached phrase bank) or an async byte-chunk iterator
(streaming TTS). Supports hard-stop flush for barge-in and reports
completion back to the arbitration queue.

INVARIANTS: 24kHz mono int16 output by default (kokoro/qwen3-tts native;
config per provider); exactly one item plays at a time; stop() is safe from
any task; playback state changes notify the half-duplex suppressor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import numpy as np

from voco.core.arbitration import PlaybackItem


class SpeakerPlayer:
    def __init__(
        self,
        on_finished: Callable[[], None],
        on_playing_changed: Callable[[bool], None] = lambda p: None,
        sample_rate: int = 24_000,
        device: int | str | None = None,
        buffer_threshold_ms: int = 150,
    ) -> None:
        self._on_finished = on_finished
        self._on_playing_changed = on_playing_changed
        self._sample_rate = sample_rate
        self._device = device
        self._buffer_threshold_ms = buffer_threshold_ms
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---- Player port (called synchronously by arbitration) ----------------

    def play(self, item: PlaybackItem) -> None:
        assert self._loop is not None, "bind_loop() before play()"
        self._task = self._loop.create_task(self._run(item))

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None

    # ---- internals ---------------------------------------------------------

    async def _run(self, item: PlaybackItem) -> None:
        try:
            self._on_playing_changed(True)
            content = item.content
            if isinstance(content, (bytes, bytearray)):
                await self._play_pcm(bytes(content))
            else:
                await self._play_stream(content)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # named fail-silent: a playback error must not kill the loop;
            # the item is reported finished and the daemon's error event
            # carries diagnostics at the call site.
        finally:
            self._on_playing_changed(False)
            if self._task is not None:
                self._task = None
                self._on_finished()

    async def _play_pcm(self, pcm: bytes) -> None:
        import sounddevice as sd

        data = np.frombuffer(pcm, dtype=np.int16)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        pos = 0

        def callback(outdata, frames, time_info, status) -> None:
            nonlocal pos
            chunk = data[pos : pos + frames]
            outdata[: len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk) :, 0] = 0
                raise sd.CallbackStop
            pos += frames

        stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=callback,
            finished_callback=lambda: loop.call_soon_threadsafe(done.set),
        )
        with stream:
            try:
                await done.wait()
            except asyncio.CancelledError:
                stream.abort()
                raise

    async def _play_stream(self, chunks: AsyncIterator[bytes]) -> None:
        """Streaming TTS: buffer to threshold, then feed the device."""
        import sounddevice as sd

        threshold = self._sample_rate * 2 * self._buffer_threshold_ms // 1000
        buffer = bytearray()  # GIL-atomic extend/del; no lock needed
        finished_feeding = False
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def callback(outdata, frames, time_info, status) -> None:
            needed = frames * 2
            take = bytes(buffer[:needed])
            del buffer[: len(take)]
            out = np.frombuffer(take, dtype=np.int16)
            outdata[: len(out), 0] = out
            if len(out) < frames:
                outdata[len(out) :, 0] = 0
                if finished_feeding:
                    raise sd.CallbackStop

        stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=callback,
            finished_callback=lambda: loop.call_soon_threadsafe(done.set),
        )
        started = False
        try:
            async for chunk in chunks:
                buffer.extend(chunk)
                if not started and len(buffer) >= threshold:
                    stream.start()
                    started = True
            finished_feeding = True
            if not started:
                if not buffer:
                    return
                stream.start()
                started = True
            await done.wait()
        finally:
            if started:
                stream.stop()
            stream.close()
