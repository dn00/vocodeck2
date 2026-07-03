"""Envelope + event vocabulary (SPEC §10).

ROLE: define the one wire shape ({v, seq, ts, type, payload}) and the legal
event/command type names, with validators strict enough to catch author
mistakes and loose enough to let v1 clients ignore unknown payload fields.
INVARIANTS: stdlib only; seq is assigned by the daemon event bus, global and
monotonic per run; no replay semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# Events the daemon emits (SPEC §10). Kept as a set, not an enum, so
# additive growth never breaks a deployed consumer.
EVENT_TYPES = {
    "snapshot",
    "session.attached",
    "session.detached",
    "session.renamed",
    "session.state",
    "session.activated",
    "stt.partial",
    "stt.final",
    "turn.state",
    "route.decision",
    "speech.started",
    "speech.interrupted",
    "speech.finished",
    "agent.say",
    "screen.updated",
    "input.queued",
    "digest.updated",
    "mic.state",
    "daemon.error",
}

# Turn-scoped events MUST carry payload.turn_id (review finding 5).
TURN_SCOPED = {
    "turn.state",
    "route.decision",
    "speech.started",
    "speech.interrupted",
    "speech.finished",
    "agent.say",
    "input.queued",
}

COMMAND_TYPES = {
    "switch_session",
    "interrupt",
    "mic.set",
    "session.spawn",
    "session.kill",
    "session.panes",
    "session.detach",
    "session.peek",
    "say_as_user",
    "state.get",
    "config.get",
    "config.set",
}


@dataclass
class Envelope:
    type: str
    payload: dict[str, Any]
    seq: int = 0
    ts: float = 0.0
    v: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
        }


def make_event(type_: str, payload: dict[str, Any]) -> Envelope:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_}")
    if type_ in TURN_SCOPED and "turn_id" not in payload:
        raise ValueError(f"turn-scoped event {type_} missing turn_id")
    return Envelope(type=type_, payload=payload)


def validate_envelope(raw: Any) -> Envelope:
    """Validate an inbound command envelope from a WS/control client."""
    if not isinstance(raw, dict):
        raise ValueError("envelope must be an object")
    v = raw.get("v", PROTOCOL_VERSION)
    if not isinstance(v, int) or v > PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {v!r}")
    type_ = raw.get("type") or raw.get("cmd")
    if not isinstance(type_, str) or type_ not in COMMAND_TYPES:
        raise ValueError(f"unknown command: {type_!r}")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return Envelope(type=type_, payload=payload)


@dataclass
class CommandReply:
    id: str | None
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.ok:
            out["payload"] = self.payload
        else:
            out["error"] = self.error or "error"
        return out
