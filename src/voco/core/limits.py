"""Shared resource limits for untrusted control-plane content."""

from __future__ import annotations

MAX_QUEUED_INPUTS = 100
MAX_INPUT_BYTES = 64 * 1024
MAX_SCREEN_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def utf8_size(text: str) -> int:
    """Return the encoded byte size used by persistence and transports."""
    return len(text.encode("utf-8"))


def screen_candidate(current: str, incoming: str, mode: str) -> str:
    """Build the exact screen value a show/append operation would store."""
    return f"{current}\n{incoming}" if mode == "append" else incoming


def validate_screen_candidate(current: str, incoming: str, mode: str) -> str:
    """Return the candidate or reject it before any state is mutated."""
    candidate = screen_candidate(current, incoming, mode)
    if utf8_size(candidate) > MAX_SCREEN_BYTES:
        raise ValueError(f"screen exceeds maximum size of {MAX_SCREEN_BYTES} bytes")
    return candidate
