"""W4 — the pty TerminalBackend (real ptys; Unix CI).

Spawn/echo/stream/replay/backpressure/kill against real /bin/sh
children — the parts a fake would just restate."""

from __future__ import annotations

import asyncio
import sys

import pytest

from voco.adapters.ptyterm import PtyBackend, PtyError, _Ring

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="pty backend is Unix-only in v1"
)


@pytest.fixture
def backend():
    b = PtyBackend()
    yield b
    b.shutdown()


async def drain_until(pp, needle: bytes, timeout: float = 5.0) -> bytes:
    """Read the ring until `needle` shows up (output is async)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if needle in pp.replay():
            return pp.replay()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{needle!r} never appeared; got {pp.replay()!r}")


async def test_spawn_echo_and_capture(backend, tmp_path):
    pp = backend.spawn("echo hello-from-pty; cat", cwd=str(tmp_path))
    out = await drain_until(pp, b"hello-from-pty")
    assert b"hello-from-pty" in out
    assert pp.alive
    assert "hello-from-pty" in pp.capture()


async def test_write_reaches_the_child(backend):
    pp = backend.spawn("cat")
    pp.write(b"round-trip\n")
    await drain_until(pp, b"round-trip")


async def test_stream_subscribers_get_live_bytes_and_eof(backend):
    pp = backend.spawn("cat")
    q = pp.subscribe()
    pp.write(b"streamed\n")
    got = b""
    while b"streamed" not in got:
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert frame is not None
        got += frame
    pp.kill()
    # After death every subscriber hears the None EOF sentinel.
    while True:
        frame = await asyncio.wait_for(q.get(), timeout=5)
        if frame is None:
            break


async def test_replay_holds_scrollback_for_late_joiners(backend):
    pp = backend.spawn("echo early-line; cat")
    await drain_until(pp, b"early-line")
    q = pp.subscribe()  # late joiner: replay has what it missed
    assert b"early-line" in pp.replay()
    pp.unsubscribe(q)


async def test_ring_buffer_bounds_memory():
    ring = _Ring(limit=100)
    for _ in range(50):
        ring.push(b"x" * 10)
    snap = ring.snapshot()
    assert len(snap) <= 100
    assert snap == b"x" * len(snap)  # oldest dropped, newest kept


async def test_subscriber_backpressure_drops_oldest(backend):
    """A stalled client's queue stays bounded: the fan-out drops its
    oldest frame instead of growing without limit or stalling the pty."""
    from voco.adapters.ptyterm import SUBSCRIBER_FRAMES

    pp = backend.spawn("cat")
    q = pp.subscribe()
    for _ in range(SUBSCRIBER_FRAMES):  # a stalled browser: full queue
        q.put_nowait(b"old")
    pp.write(b"fresh-frame\n")
    await drain_until(pp, b"fresh-frame")
    assert q.qsize() <= SUBSCRIBER_FRAMES  # bounded, not grown
    frames: list[bytes] = []
    while not q.empty():
        f = q.get_nowait()
        if f:
            frames.append(f)
    assert any(b"fresh-frame" in f for f in frames)  # newest survived


async def test_kill_ends_process_and_closes(backend):
    pp = backend.spawn("cat")
    handle = pp.handle
    assert pp.alive
    backend.kill(handle)
    assert not pp.alive
    assert backend.get(handle) is None
    with pytest.raises(PtyError):
        pp.write(b"nope")


async def test_child_exit_closes_streams(backend):
    pp = backend.spawn("echo bye")  # exits on its own
    q = pp.subscribe()
    while True:
        frame = await asyncio.wait_for(q.get(), timeout=5)
        if frame is None:
            break
    assert not pp.alive


async def test_resize_reaches_the_child(backend):
    pp = backend.spawn("cat")
    pp.resize(cols=99, rows=24)  # no raise = ioctl accepted
    pp.write(b"still-works\n")
    await drain_until(pp, b"still-works")


async def test_kill_unknown_handle_raises(backend):
    with pytest.raises(PtyError, match="no such terminal"):
        backend.kill("pty-404")


async def test_spawned_env_and_cwd(backend, tmp_path):
    pp = backend.spawn(
        "pwd; echo $VOCO_TEST", cwd=str(tmp_path), env={"VOCO_TEST": "cell-check"}
    )
    out = await drain_until(pp, b"cell-check")
    assert str(tmp_path).encode() in out


async def test_natural_exit_deregisters_from_backend(backend):
    """Short-lived commands must not accumulate dead handles: a natural
    exit removes the terminal from the backend, so /v1/term answers 404
    instead of a closed stream (Codex W3-W5 review, WARNING 4)."""
    pp = backend.spawn("echo done")
    q = pp.subscribe()
    while (await asyncio.wait_for(q.get(), timeout=5)) is not None:
        pass
    assert backend.get(pp.handle) is None
