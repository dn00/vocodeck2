"""Global push-to-talk hotkey (SPEC §4.4).

ROLE: capture press AND release of the PTT key system-wide and forward them
to the turn machine's shell, thread-safely. Mechanisms per SPEC: pynput
(Windows low-level hook / macOS CGEventTap / X11). Wayland: unsupported —
construction raises and the daemon logs the documented fallback.

INVARIANTS: optional dependency — importing this module without pynput must
not crash the daemon (capability degrades, PTT off); callbacks are marshaled
onto the asyncio loop.
"""

from __future__ import annotations

import asyncio
from typing import Callable

DEFAULT_KEY = "f9"


class PttHotkey:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        key: str = DEFAULT_KEY,
    ) -> None:
        try:
            from pynput import keyboard  # noqa: PLC0415
        except ImportError as e:  # capability degrades, never crashes (§1.2)
            raise RuntimeError(
                "pynput not installed — PTT disabled (uv sync --extra ptt)"
            ) from e
        self._keyboard = keyboard
        self._loop = loop
        self._on_press = on_press
        self._on_release = on_release
        self._target = self._parse(key)
        self._down = False
        self._listener: object | None = None

    def _parse(self, key: str):
        kb = self._keyboard
        try:
            return getattr(kb.Key, key.lower())
        except AttributeError:
            return kb.KeyCode.from_char(key)

    def start(self) -> None:
        kb = self._keyboard

        def press(key) -> None:
            if key == self._target and not self._down:
                self._down = True
                self._loop.call_soon_threadsafe(self._on_press)

        def release(key) -> None:
            if key == self._target and self._down:
                self._down = False
                self._loop.call_soon_threadsafe(self._on_release)

        self._listener = kb.Listener(on_press=press, on_release=release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
