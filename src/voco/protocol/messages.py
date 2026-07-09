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
    "turn.patience",
    "route.decision",
    "speech.started",
    "speech.interrupted",
    "speech.finished",
    "speech.expired",
    # Per-sentence playback progress (DESIGN-DECK U0): emitted as the
    # player pulls into each sentence of an agent say — drives the
    # transcript's karaoke highlight. Not turn-scoped: it is playback
    # progress, not a turn lifecycle edge.
    "speech.sentence",
    "agent.say",
    "screen.updated",
    "input.queued",
    "digest.updated",
    "pane.hint",
    "mic.state",
    "daemon.error",
    # Workbench (SPEC-WORKBENCH §9). screen.updated stays alongside
    # page.updated for screen pages — exact legacy payload, kept.
    "workspace.updated",
    "page.updated",
    "finding.added",
    "finding.updated",
    "ask.created",
    "ask.answered",
    "term.opened",
    "term.closed",
}

# Turn-scoped events MUST carry payload.turn_id (review finding 5).
TURN_SCOPED = {
    "turn.state",
    "turn.patience",
    "route.decision",
    "speech.started",
    "speech.interrupted",
    "speech.finished",
    "speech.expired",
    "agent.say",
    "input.queued",
}

COMMAND_TYPES = {
    "switch_session",
    "interrupt",
    "mic.set",
    # mk3.1: client hold-PTT — the deck's hold button / key ride the
    # same turn-machine path as the native hotkey.
    "ptt.press",
    "ptt.release",
    "session.spawn",
    "session.kill",
    "session.panes",
    "session.detach",
    "session.peek",
    "say_as_user",
    "state.get",
    "config.get",
    "config.set",
    # Workbench (SPEC-WORKBENCH §9): browser mutations ride commands so
    # the debug UI and tests reach them too.
    "workspace.list",
    "workspace.live",
    # DESIGN-DECK U0: agentless review + the transcript read path.
    "workspace.open",
    "page.publish",
    "session.transcript",
    # DESIGN-DECK rev 5 (U2a): GitHub links + the connect modal's snippet.
    "workspace.link",
    "attach.snippet",
    # U2c: human status path — undo-over-confirm needs re-open.
    "finding.status",
    # B1c: the file viewer's tracked-file list.
    "workspace.files",
    "page.close",
    "page.reopen",
    "finding.list",
    "finding.add",
    "finding.update",
    "finding.withdraw",
    "ask.create",
    "ask.list",
    "review.export",
    "review.primary",
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
