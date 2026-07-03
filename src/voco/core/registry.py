"""Session registry (SPEC §8.2–§8.3).

ROLE: owns session records — derived identity, call names, capability
tokens, parked/working/idle state, per-session input queues, say logs,
digests, screen content. Transport-free: delivery to a parked listener goes
through an injected `try_deliver` port (the bridge implements it).

INVARIANTS:
- session_id is an unguessable capability token (128-bit hex).
- `working` never times out into stale (review finding 3); stale is a
  display flag computed from idle time only.
- Exactly one active session or none; no auto-election on detach.
- Dispatch to a non-parked session queues; queues are in-memory (v1,
  documented loss on restart).
- Call names come from a phonetically distinct pool, stable per identity.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

NAME_POOL = [
    "Helena",
    "Marcus",
    "Iris",
    "Felix",
    "Nova",
    "Orion",
    "Dana",
    "Silas",
    "Petra",
    "Leo",
    "Wanda",
    "Otto",
    "Zara",
    "Hugo",
    "Freya",
    "Ezra",
]

SessionState = Literal["parked", "working", "idle"]


@dataclass
class SayLine:
    ts: float
    text: str
    turn_id: str | None


@dataclass
class QueuedInput:
    ts: float
    turn_id: str
    text: str


@dataclass
class Session:
    session_id: str
    identity: dict[str, Any]  # host, user, cwd, repo, branch, harness, pid
    call_name: str
    capabilities: list[str]
    parked: bool = False
    outstanding_turn_id: str | None = None
    queued: list[QueuedInput] = field(default_factory=list)
    say_log: deque[SayLine] = field(default_factory=lambda: deque(maxlen=50))
    unread_digest: int = 0
    screen_title: str | None = None
    screen_markdown: str = ""
    last_seen: float = 0.0

    @property
    def state(self) -> SessionState:
        if self.parked:
            return "parked"
        if self.outstanding_turn_id is not None:
            return "working"
        return "idle"

    @property
    def inject_target(self) -> str | None:
        """tmux pane id (adapter-derived) or spawned tmux session name."""
        return self.identity.get("tmux_pane") or self.identity.get("tmux_session")

    @property
    def display_name(self) -> str:
        host = self.identity.get("host", "?")
        cwd = str(self.identity.get("cwd", "?")).rstrip("/").split("/")[-1]
        return f"{self.call_name} ({host}:{cwd})"


DispatchResult = Literal["live", "queued", "queued_idle", "no_session"]


class Registry:
    def __init__(
        self,
        emit: Callable[[str, dict], object] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._emit = emit or (lambda t, p: None)
        self._now = now
        self._sessions: dict[str, Session] = {}
        self._by_identity: dict[tuple, str] = {}
        self._active_id: str | None = None
        self._turn_counter = 0
        # The bridge implements live delivery to a parked long-poll.
        self.try_deliver: Callable[[str, dict], bool] = lambda sid, payload: False

    # ---- registration ----------------------------------------------------

    def register(self, identity: dict[str, Any], capabilities: list[str]) -> Session:
        key = (
            identity.get("host"),
            identity.get("cwd"),
            identity.get("harness"),
        )
        existing_id = self._by_identity.get(key)
        if existing_id is not None and existing_id in self._sessions:
            s = self._sessions[existing_id]
            s.identity.update(identity)  # refresh derived git facts
            s.last_seen = self._now()
            return s
        capabilities = list(capabilities)
        if (identity.get("tmux_pane") or identity.get("tmux_session")) and (
            "inject" not in capabilities
        ):
            capabilities.append("inject")
        s = Session(
            session_id=secrets.token_hex(16),
            identity=dict(identity),
            call_name=self._assign_name(key),
            capabilities=capabilities,
            last_seen=self._now(),
        )
        self._sessions[s.session_id] = s
        self._by_identity[key] = s.session_id
        if len(self._sessions) == 1:
            self._active_id = s.session_id  # auto-activate the only session
            self._emit("session.activated", {"session_id": s.session_id})
        self._emit(
            "session.attached",
            {"session_id": s.session_id, "name": s.display_name},
        )
        return s

    def _assign_name(self, key: tuple) -> str:
        taken = {s.call_name for s in self._sessions.values()}
        digest = hashlib.sha256(repr(key).encode()).digest()
        start = digest[0] % len(NAME_POOL)
        for i in range(len(NAME_POOL)):
            name = NAME_POOL[(start + i) % len(NAME_POOL)]
            if name not in taken:
                return name
        return f"Agent{len(self._sessions) + 1}"

    # ---- lookups -----------------------------------------------------------

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def call_names(self) -> list[str]:
        return [s.call_name for s in self._sessions.values()]

    @property
    def active(self) -> Session | None:
        return self._sessions.get(self._active_id) if self._active_id else None

    def by_call_name(self, name: str) -> Session | None:
        for s in self._sessions.values():
            if s.call_name.lower() == name.lower():
                return s
        return None

    # ---- activation --------------------------------------------------------

    def switch(self, name: str) -> Session | None:
        s = self.by_call_name(name)
        if s is None:
            return None
        self._active_id = s.session_id
        s.unread_digest = 0
        self._emit("session.activated", {"session_id": s.session_id})
        return s

    def detach(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return
        # A parked listener exits cleanly instead of timing out into a 410.
        self.try_deliver(session_id, {"status": "detach"})
        self._by_identity.pop(
            (s.identity.get("host"), s.identity.get("cwd"), s.identity.get("harness")),
            None,
        )
        self._emit("session.detached", {"session_id": session_id})
        if self._active_id == session_id:
            # No auto-election (SPEC §5.4 rule 6).
            self._active_id = None

    # ---- dispatch (SPEC §8.1/§8.2) ------------------------------------------

    def mint_turn_id(self) -> str:
        self._turn_counter += 1
        return f"t-{self._turn_counter}"

    def dispatch(
        self, text: str, turn_id: str, target: Session | None = None
    ) -> DispatchResult:
        s = target or self.active
        if s is None:
            return "no_session"
        was_idle = s.state == "idle"
        payload = {
            "status": "transcript",
            "turn_id": turn_id,
            "text": text,
            "queued": [q.__dict__ for q in s.queued],
        }
        if s.parked and self.try_deliver(s.session_id, payload):
            s.queued.clear()
            s.parked = False
            s.outstanding_turn_id = turn_id
            self._emit("session.state", {"session_id": s.session_id, "state": s.state})
            return "live"
        s.queued.append(QueuedInput(ts=self._now(), turn_id=turn_id, text=text))
        self._emit(
            "input.queued",
            {"session_id": s.session_id, "turn_id": turn_id, "text": text},
        )
        return "queued_idle" if was_idle else "queued"

    # ---- bridge hooks --------------------------------------------------------

    def on_listen_start(self, session_id: str) -> dict | None:
        """Bridge parks a poll. Returns an immediate payload if input waits."""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        s.last_seen = self._now()
        s.outstanding_turn_id = None  # a new listen ends the working turn
        if s.queued:
            first, rest = s.queued[0], s.queued[1:]
            payload = {
                "status": "transcript",
                "turn_id": first.turn_id,
                "text": first.text,
                "queued": [q.__dict__ for q in rest],
            }
            s.queued.clear()
            s.outstanding_turn_id = first.turn_id
            return payload
        s.parked = True
        self._emit("session.state", {"session_id": session_id, "state": s.state})
        return None

    def on_listen_end(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s is not None:
            s.parked = False

    def record_say(self, session_id: str, text: str, turn_id: str | None) -> bool:
        """Returns True if this session is active (say should be spoken)."""
        s = self._sessions.get(session_id)
        if s is None:
            return False
        line = SayLine(ts=self._now(), text=text, turn_id=turn_id)
        s.say_log.append(line)
        is_active = self._active_id == session_id
        if not is_active:
            s.unread_digest += 1
            self._emit(
                "digest.updated",
                {"session_id": session_id, "unread": s.unread_digest},
            )
        self._emit(
            "agent.say",
            {
                "session_id": session_id,
                "text": text,
                "turn_id": turn_id or s.outstanding_turn_id,
                "active": is_active,
            },
        )
        return is_active

    def set_screen(
        self, session_id: str, markdown: str, title: str | None, mode: str
    ) -> None:
        s = self._sessions.get(session_id)
        if s is None:
            return
        if mode == "append":
            s.screen_markdown += "\n" + markdown
        else:
            s.screen_markdown = markdown
            s.screen_title = title
        # Full current content rides along so UIs render without a refetch.
        self._emit(
            "screen.updated",
            {
                "session_id": session_id,
                "title": s.screen_title,
                "markdown": s.screen_markdown,
            },
        )

    # ---- snapshot (SPEC §10) ---------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "name": s.call_name,
                    "display_name": s.display_name,
                    "state": s.state,
                    "capabilities": s.capabilities,
                    "unread_digest": s.unread_digest,
                    "queued": len(s.queued),
                    "screen_title": s.screen_title,
                    "screen_markdown": s.screen_markdown,
                    "say_tail": [
                        {"ts": line.ts, "text": line.text}
                        for line in list(s.say_log)[-10:]
                    ],
                    "last_seen": s.last_seen,
                }
                for s in self._sessions.values()
            ],
            "active_session": self._active_id,
        }
