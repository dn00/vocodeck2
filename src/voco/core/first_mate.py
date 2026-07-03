"""The first-mate contract — grounding, prompt, parsing, coercion (SPEC §7).

ROLE: everything about the first-mate tier that is NOT the model call:
build the grounding block from daemon-observable facts, define the contract
system prompt, and parse/validate/coerce the model's JSON into a
RouteDecision + optional action. The model adapter (adapters/first_mate.py)
only transports; this module is pure.

INVARIANTS:
- Partition of authority is enforced structurally where possible: actions
  are validated against the closed verb set and the live roster; unknown
  actions/targets are DROPPED, never guessed (SPEC §7.2).
- Parsing is forgiving on wrapping (JSON extracted from prose) and strict
  on content; anything unusable returns None so the router coerces to
  `forward` (SPEC §7.3).
- The grounding block contains only registry facts and attributed say
  lines with ages — nothing the model could mistake for its own knowledge.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from voco.core.phrases import resolve_name
from voco.core.registry import Registry
from voco.core.turn import RouteDecision

ACTIONS = {"switch_session", "mute", "unmute", "mic_mode", "read_digest"}


class FirstMatePort(Protocol):
    """The tier the Router consults (SPEC §7). None = decline → forward."""

    async def route(self, text: str, grounding: dict) -> RouteDecision | None: ...


ROUTES = {"answer", "forward", "ack_forward"}
MAX_SAY_LINES = 5

SYSTEM_PROMPT = """\
You are VOCO, the first mate of a voice control deck for coding agents.
The user speaks; real work is done by agent sessions (listed in the
grounding block). You operate the switchboard and keep the user company.

YOUR AUTHORITY — you may ONLY speak about:
1. The loop itself: receipt, routing, which session is active, how long an
   agent has been working. Example: "Sent that to Helena."
2. What agents SAID, always attributed with age, never as your own claim.
   Example: "Helena said two tests failed, about a minute ago."
3. Light social conversation, in character, with zero claims about the work.

You must NEVER: state facts about code or results in your own voice,
predict outcomes, promise what an agent will do ("on it — refactoring" is
banned; "sending that over" is your ceiling), rephrase or summarize the
user's words when forwarding, or answer technical/work questions yourself —
those are forwarded verbatim. Questions about code, files, tests, bugs, or
architecture are ALWAYS work questions, even when they look like simple
facts ("which file has X", "is Y thread safe") — forward them; you do not
know the codebase, only the loop.

OUTPUT: exactly one JSON object, nothing else:
{"route": "answer" | "forward" | "ack_forward",
 "speech": "<1-2 short plain sentences, no markdown>",
 "target": null | "<call name>",
 "action": null | {"type": "switch_session"|"mute"|"unmute"|
                   "mic_mode"|"read_digest", ...}}

- route "forward": the utterance is for the agent. speech may be "" or a
  brief routing ack. Use "target" ONLY when the user names a session
  ("tell Helena ..."); otherwise null (active session).
- route "ack_forward": forward AND speak a short routing ack. This is your
  DEFAULT for anything work-related. When unsure, choose this.
- route "answer": ONLY for loop questions, attributed-log questions, or
  social talk. speech must be non-empty.
- action: only when the user asked for that exact operation. switch_session
  needs {"type":"switch_session","target":"<call name>"}; mic_mode needs
  {"type":"mic_mode","mode":"full_duplex"|"half_duplex"}. Asking ABOUT a
  session ("did Marcus finish?") is a question, not a switch — no action,
  and if the answer is in that session's recent says, route "answer" with
  attribution ("Marcus said ..., five minutes ago").
"""


_FORWARD_PREFIX = re.compile(r"^\s*(?:tell|ask)\s+([a-z]+)\b", re.IGNORECASE)


def fallback_target(text: str, roster: list[str]) -> str | None:
    """Misroute guard for mate-tier deadline misses: when the mate is
    configured but times out, 'tell/ask <known name> ...' keeps its spoken
    destination instead of silently landing on the active session.
    Registry facts + conservative fuzz only — this is NOT targeted
    forwarding for degraded mode (SPEC §14.9: no mate, no targets)."""
    m = _FORWARD_PREFIX.match(text)
    if m is None:
        return None
    return resolve_name(m.group(1), roster)


def build_grounding(registry: Registry, mic_mode: str, now: float) -> dict[str, Any]:
    sessions = []
    for s in registry.all():
        says = [
            {"age_s": round(max(0.0, now - line.ts)), "text": line.text}
            for line in list(s.say_log)[-MAX_SAY_LINES:]
        ]
        sessions.append(
            {
                "name": s.call_name,
                "repo": s.identity.get("repo"),
                "branch": s.identity.get("branch"),
                "harness": s.identity.get("harness"),
                "state": s.state,
                "unread_digest": s.unread_digest,
                "queued_inputs": len(s.queued),
                "recent_says": says,
            }
        )
    active = registry.active
    return {
        "sessions": sessions,
        "active_session": active.call_name if active else None,
        "mic_mode": mic_mode,
    }


def _extract_json(text: str) -> dict | None:
    """First {...} block, tolerant of prose/markdown fences around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_decision(raw: str, roster: list[str]) -> RouteDecision | None:
    """Model text → validated RouteDecision, or None (router → forward)."""
    obj = _extract_json(raw)
    if obj is None:
        return None
    route = obj.get("route")
    if route not in ROUTES:
        return None
    speech = obj.get("speech")
    speech = speech.strip() if isinstance(speech, str) else ""

    target = None
    raw_target = obj.get("target")
    if isinstance(raw_target, str) and raw_target.strip():
        # Unknown targets are dropped → active session (never guessed).
        target = resolve_name(raw_target, roster)

    action = _validate_action(obj.get("action"), roster)
    return RouteDecision(kind=route, speech=speech, target=target, action=action)


def _validate_action(raw: Any, roster: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    type_ = raw.get("type")
    if type_ not in ACTIONS:
        return None
    if type_ == "switch_session":
        target = resolve_name(str(raw.get("target", "")), roster)
        if target is None:
            return None
        return {"type": type_, "target": target}
    if type_ == "mic_mode":
        mode = raw.get("mode")
        if mode not in ("full_duplex", "half_duplex"):
            return None
        return {"type": type_, "mode": mode}
    if type_ == "read_digest":
        target = resolve_name(str(raw.get("target", "")), roster)
        return {"type": type_, "target": target}  # None target = active
    return {"type": type_}


def execute_action(
    action: dict,
    registry: Registry,
    set_mic: Callable[[str], None],
    set_muted: Callable[[bool], None],
) -> None:
    """Daemon-side action execution — code is law (SPEC §7.3)."""
    type_ = action["type"]
    if type_ == "switch_session":
        registry.switch(action["target"])
    elif type_ == "mute":
        set_muted(True)
    elif type_ == "unmute":
        set_muted(False)
    elif type_ == "mic_mode":
        set_mic(action["mode"])
    elif type_ == "read_digest":
        name = action.get("target")
        s = registry.by_call_name(name) if name else registry.active
        if s is not None:
            s.unread_digest = 0
