"""Async lifecycle coverage for the real speaker playback adapter."""

from __future__ import annotations

import asyncio

import pytest

from voco.adapters.speaker import SpeakerPlayer
from voco.core.arbitration import PlaybackItem, PlaybackQueue, Source


@pytest.mark.asyncio
async def test_preempted_task_cannot_finish_its_replacement() -> None:
    """A cancelled item may unwind after arbitration starts the next one."""
    playing: list[bool] = []
    started: list[bytes] = []
    releases = [asyncio.Event(), asyncio.Event()]
    player: SpeakerPlayer

    async def play_pcm(pcm: bytes) -> None:
        index = len(started)
        started.append(pcm)
        await releases[index].wait()

    player = SpeakerPlayer(
        on_finished=lambda: queue.on_item_finished(),
        on_playing_changed=playing.append,
    )
    player._play_pcm = play_pcm  # type: ignore[method-assign]
    player.bind_loop(asyncio.get_running_loop())
    queue = PlaybackQueue(player)
    queue.note_dispatch("t-7")

    queue.enqueue(PlaybackItem(Source.FIRST_MATE, b"first", turn_id="t-7"))
    await asyncio.sleep(0)
    queue.enqueue(PlaybackItem(Source.AGENT, b"second", turn_id="t-7"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    replacement = player._task
    assert started == [b"first", b"second"]
    assert replacement is not None and not replacement.done()
    assert player._task is replacement
    assert queue._playing is not None
    assert queue._playing.source is Source.AGENT

    releases[1].set()
    await replacement
    assert player._task is None
    assert queue._playing is None
    assert playing == [True, False, True, False]


@pytest.mark.asyncio
async def test_playback_error_is_reported_and_queue_advances() -> None:
    errors: list[Exception] = []
    finished: list[bool] = []

    async def fail(_pcm: bytes) -> None:
        raise RuntimeError("output device disappeared")

    player = SpeakerPlayer(
        on_finished=lambda: finished.append(True),
        on_error=errors.append,
    )
    player._play_pcm = fail  # type: ignore[method-assign]
    player.bind_loop(asyncio.get_running_loop())
    player.play(PlaybackItem(Source.AGENT, b"audio"))
    task = player._task
    assert task is not None
    await task

    assert len(errors) == 1
    assert str(errors[0]) == "output device disappeared"
    assert finished == [True]
    assert player._task is None
