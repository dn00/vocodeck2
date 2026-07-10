"""Input Monitoring preflight (BUILD-PROD P4): the probe is tri-state —
True/False are real answers, None means "cannot tell" and must never be
read as denied (that policy is pinned in test_voice_loop.py)."""

from __future__ import annotations

import sys

from voco.adapters.hotkey import input_monitoring_granted


def test_non_darwin_cannot_tell(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert input_monitoring_granted() is None


def test_darwin_answers_or_admits_ignorance():
    """On macOS the real probe runs (CGPreflightListenEventAccess never
    prompts); anywhere it can't load the symbol it must say None, not
    guess. Environment-dependent by nature, so pin the CONTRACT."""
    result = input_monitoring_granted()
    if sys.platform == "darwin":
        assert result is None or isinstance(result, bool)
    else:
        assert result is None
