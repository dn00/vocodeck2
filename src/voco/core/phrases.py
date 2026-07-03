"""Phrase table (SPEC §6).

ROLE: the deterministic, safety-critical control path — exact/fuzzy matching
of hard commands, zero models, ~0ms. Also the entire routing layer when the
Gemma tier is off (degraded mode: phrase hit or forward-verbatim).

INVARIANTS: never matches open-ended instructions (no similarity scoring
against free text — the v1 behavior-router lesson); switch targets resolve
against the live call-name list with conservative fuzz.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Literal

PhraseKind = Literal["stop", "mute", "unmute", "switch", "sessions"]

_STOP = {"stop", "cancel", "stop that", "cancel that", "never mind", "nevermind"}
_MUTE = {"mute", "be quiet", "quiet", "shut up", "silence"}
_UNMUTE = {"unmute", "start listening", "listen again"}
_SESSIONS = {
    "what sessions are connected",
    "which sessions are connected",
    "list sessions",
    "who is connected",
    "who's connected",
}
_SWITCH_PREFIXES = ("switch to ", "talk to ", "go to ", "connect me to ")

_NAME_FUZZ = 0.75  # conservative: prefer no-match over wrong session


@dataclass
class PhraseCommand:
    kind: PhraseKind
    target: str | None = None  # resolved call name for switch


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s']", "", text)
    return re.sub(r"\s+", " ", text)


def resolve_name(spoken: str, names: list[str]) -> str | None:
    spoken = _normalize(spoken)
    if not spoken or not names:
        return None
    by_lower = {n.lower(): n for n in names}
    if spoken in by_lower:
        return by_lower[spoken]
    match = difflib.get_close_matches(spoken, list(by_lower), n=1, cutoff=_NAME_FUZZ)
    return by_lower[match[0]] if match else None


def match(text: str, names: list[str]) -> PhraseCommand | None:
    t = _normalize(text)
    if not t:
        return None
    if t in _STOP:
        return PhraseCommand("stop")
    if t in _MUTE:
        return PhraseCommand("mute")
    if t in _UNMUTE:
        return PhraseCommand("unmute")
    if t in _SESSIONS:
        return PhraseCommand("sessions")
    for prefix in _SWITCH_PREFIXES:
        if t.startswith(prefix):
            name = resolve_name(t[len(prefix) :], names)
            if name is not None:
                return PhraseCommand("switch", target=name)
            return None  # spoke a switch, named nobody we know: not a command
    return None
