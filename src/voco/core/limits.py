"""Shared resource limits for untrusted control-plane content."""

from __future__ import annotations

MAX_QUEUED_INPUTS = 100
MAX_INPUT_BYTES = 64 * 1024


def utf8_size(text: str) -> int:
    """Return the encoded byte size used by persistence and transports."""
    return len(text.encode("utf-8"))
