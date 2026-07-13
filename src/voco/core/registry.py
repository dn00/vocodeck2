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
- Dispatch to a non-parked session queues; queues are persisted, while a
  restored session remains unroutable until its cached agent calls back in.
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

from voco.core.limits import (
    MAX_INPUT_BYTES,
    MAX_QUEUED_INPUTS,
    MAX_SCREEN_BYTES,
    utf8_size,
    validate_screen_candidate,
)

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
    origin: str = "voice"  # voice | typed (UI box / `voco input`)


@dataclass
class InputLine:
    """One dispatched user utterance (DESIGN-DECK U0): the user half of
    the transcript, symmetric to SayLine. `queued` records whether it
    waited for the agent (the transcript renders that as a meta line)."""

    ts: float
    text: str
    origin: str = "voice"  # voice | typed
    queued: bool = False


@dataclass
class Session:
    session_id: str
    identity: dict[str, Any]  # host, user, cwd, repo, branch, harness, pid
    call_name: str
    capabilities: list[str]
    parked: bool = False
    outstanding_turn_id: str | None = None
    # Working on delivered review items (ephemeral, not persisted): review
    # wakes mint no turn_id, but the agent IS busy — state must say so or
    # voice input during review work reads as queued-to-idle and nudges.
    reviewing: bool = False
    # Persisted command backlog. New commands are rejected once this reaches
    # MAX_QUEUED_INPUTS; accepted commands are never silently discarded.
    queued: list[QueuedInput] = field(default_factory=list)
    say_log: deque[SayLine] = field(default_factory=lambda: deque(maxlen=50))
    # The user half of the transcript (DESIGN-DECK U0): same bound, same
    # persistence as say_log; recorded at dispatch.
    input_log: deque[InputLine] = field(default_factory=lambda: deque(maxlen=50))
    unread_digest: int = 0
    screen_title: str | None = None
    screen_markdown: str = ""
    last_seen: float = 0.0
    # Watcher observation (ephemeral, not persisted): waiting|working|shell.
    pane_hint: str | None = None
    # Last session.state actually emitted (ephemeral): dedupe — a listen
    # slice re-parking every 50s must not spam identical events.
    last_state_emitted: str | None = None
    # Ephemeral transport truth. Restored tokens begin disconnected and only
    # become routing targets after the cached agent actually calls back in.
    connected: bool = True

    @property
    def state(self) -> SessionState:
        if self.parked:
            return "parked"
        if self.outstanding_turn_id is not None or self.reviewing:
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

    @property
    def home_root(self) -> str | None:
        """Where this session lives: the checkout root, else cwd — the
        same rule WorkspaceStore.resolve/home_of applies. Rides snapshot
        + session.attached so UIs can scope sessions to workspaces."""
        root = self.identity.get("worktree") or self.identity.get("cwd")
        return str(root) if root else None


DispatchResult = Literal["live", "queued", "queued_idle", "no_session"]


def _identity_key(identity: dict[str, Any]) -> tuple:
    """Who is 'the same agent' across re-registrations. The instance
    component (tmux pane / harness session id, client-derived) keeps two
    agents in one cwd from collapsing into one session (live-test bug);
    clients that send none keep the legacy coarse key."""
    return (
        identity.get("host"),
        identity.get("cwd"),
        identity.get("harness"),
        identity.get("instance"),
    )


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
        # Tombstones for THIS daemon run: a listener that missed the live
        # detach delivery must hear "detach", never a 410 that would make
        # it resurrect the session the user just ended (live-test bug).
        self._detached: set[str] = set()
        # The bridge implements live delivery to a parked long-poll.
        self.try_deliver: Callable[[str, dict], bool] = lambda sid, payload: False
        # Pending review items (SPEC-WORKBENCH §4.2) for a session, computed
        # by the daemon from the workspace ledger. Injected; default: none.
        # At-least-once: recomputed every listen, so an agent that crashes
        # mid-wake sees them again; idempotent by item id.
        self.review_items: Callable[[str], list[dict]] = lambda sid: []
        # Terminal capability cells (SPEC-WORKBENCH §5) for a session,
        # derived by the daemon from transport facts. Injected; None =
        # no managed terminal. Rides the snapshot so UIs degrade per-cell.
        self.term_cells: Callable[[Session], dict | None] = lambda s: None
        # Managed-terminal liveness for the display-state derivation
        # (§6): daemon-injected; None = unmanaged/unknown.
        self.handle_alive: Callable[[Session], bool | None] = lambda s: None

    # ---- registration ----------------------------------------------------

    # Predecessor sweep windows (same host+cwd+harness, new instance):
    # an unparked-IDLE sibling silent longer than one listen slice is a
    # corpse (a live agent parks within its first slice); an unparked-
    # WORKING one gets the long window (mid-turn agents go quiet).
    SWEEP_IDLE_S = 60.0
    SWEEP_WORKING_S = 900.0

    def register(self, identity: dict[str, Any], capabilities: list[str]) -> Session:
        key = _identity_key(identity)
        existing_id = self._by_identity.get(key)
        if existing_id is not None and existing_id in self._sessions:
            s = self._sessions[existing_id]
            s.identity.update(identity)  # refresh derived git facts
            if capabilities:
                # The adapter knows its CURRENT verbs — stale capability
                # lists (pre-review sessions) must not survive re-register.
                s.capabilities = self._with_inject(identity, capabilities)
            s.last_seen = self._now()
            self._mark_connected(s)
            return s
        s = Session(
            session_id=secrets.token_hex(16),
            identity=dict(identity),
            call_name=self._assign_name(key),
            capabilities=self._with_inject(identity, capabilities),
            last_seen=self._now(),
        )
        self._sessions[s.session_id] = s
        self._by_identity[key] = s.session_id
        self._sweep_predecessors(s)
        if len(self._sessions) == 1:
            self._active_id = s.session_id  # auto-activate the only session
            self._emit("session.activated", {"session_id": s.session_id})
        self._emit(
            "session.attached",
            {
                "session_id": s.session_id,
                "name": s.display_name,
                "capabilities": s.capabilities,
                "host": s.identity.get("host"),
                "root": s.home_root,
            },
        )
        return s

    def refresh_identity(
        self, session_id: str, identity: dict[str, Any]
    ) -> Session | None:
        """Re-assert a live session's identity from the adapter's CURRENT
        facts (dogfood 2026-07-06: a session restored/cached with a dead
        cwd made every workspace verb resolve against the wrong root).
        Keeps the session_id — the adapter still holds it — and re-keys
        the identity map so a later re-register from the new cwd finds
        the same session."""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        old_key = _identity_key(s.identity)
        old_root = s.home_root
        was_connected = s.connected
        s.identity.update(identity)
        new_key = _identity_key(s.identity)
        if new_key != old_key:
            if self._by_identity.get(old_key) == session_id:
                del self._by_identity[old_key]
            self._by_identity[new_key] = session_id  # newest claim wins
        s.last_seen = self._now()
        if s.home_root != old_root and was_connected:
            # The rail groups sessions by home root — tell UIs it moved
            # (same upsert event a fresh attach emits).
            self._emit(
                "session.attached",
                {
                    "session_id": s.session_id,
                    "name": s.display_name,
                    "capabilities": s.capabilities,
                    "host": s.identity.get("host"),
                    "root": s.home_root,
                },
            )
        self._mark_connected(s)
        return s

    def _mark_connected(self, s: Session) -> None:
        if s.connected:
            return
        s.connected = True
        s.last_seen = self._now()
        self._emit(
            "session.attached",
            {
                "session_id": s.session_id,
                "name": s.display_name,
                "capabilities": s.capabilities,
                "host": s.identity.get("host"),
                "root": s.home_root,
            },
        )
        if self._active_id == s.session_id:
            self._emit("session.activated", {"session_id": s.session_id})

    @staticmethod
    def _with_inject(identity: dict[str, Any], capabilities: list[str]) -> list[str]:
        caps = list(capabilities)
        has_terminal = (
            identity.get("tmux_pane")
            or identity.get("tmux_session")
            # Daemon-owned pty (W4): the spawn baked its handle into the
            # instance; writing to it is the inject transport.
            or str(identity.get("instance") or "").startswith("pty-")
        )
        if has_terminal and "inject" not in caps:
            caps.append("inject")
        return caps

    def _sweep_predecessors(self, newcomer: Session) -> None:
        """A fresh instance in the SAME (host, cwd, harness) marks older,
        long-silent, unparked siblings as predecessors (a resumed Claude
        session gets a new harness session id → a new identity — live-test
        bug: every resume left a grey corpse that could still hold the
        voice-active slot and hijack ask/wake routing). Detach them; if
        one held active, the newcomer inherits it (registering where the
        corpse lived is the clearest possible user intent)."""
        coarse = (
            newcomer.identity.get("host"),
            newcomer.identity.get("cwd"),
            newcomer.identity.get("harness"),
        )
        now = self._now()
        inherited_active = False
        for s in list(self._sessions.values()):
            if s.session_id == newcomer.session_id:
                continue
            if (
                s.identity.get("host"),
                s.identity.get("cwd"),
                s.identity.get("harness"),
            ) != coarse:
                continue
            if s.parked:
                continue  # a LIVE sibling (two agents, one cwd) is parked
            silent = now - s.last_seen
            window = self.SWEEP_WORKING_S if s.state == "working" else self.SWEEP_IDLE_S
            if silent <= window:
                continue  # fresh enough to be alive; leave it
            if self._active_id == s.session_id:
                inherited_active = True
            self.detach(s.session_id)
        if inherited_active:
            self._active_id = newcomer.session_id
            self._emit("session.activated", {"session_id": newcomer.session_id})

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
        s = self._sessions.get(self._active_id) if self._active_id else None
        return s if s is not None and s.connected else None

    def by_call_name(self, name: str) -> Session | None:
        for s in self._sessions.values():
            if s.call_name.lower() == name.lower():
                return s
        return None

    # ---- activation --------------------------------------------------------

    def switch(self, name: str) -> Session | None:
        s = self.by_call_name(name)
        if s is None or not s.connected:
            return None
        self._active_id = s.session_id
        s.unread_digest = 0
        self._emit("session.activated", {"session_id": s.session_id})
        return s

    def detach(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return
        self._detached.add(session_id)
        # A parked listener exits cleanly instead of timing out into a 410;
        # reason lets clients tell "user ended me" from "daemon going down".
        self.try_deliver(session_id, {"status": "detach", "reason": "detached"})
        self._by_identity.pop(_identity_key(s.identity), None)
        self._emit("session.detached", {"session_id": session_id})
        if self._active_id == session_id:
            # No auto-election (SPEC §5.4 rule 6).
            self._active_id = None

    def was_detached(self, session_id: str) -> bool:
        return session_id in self._detached

    # ---- dispatch (SPEC §8.1/§8.2) ------------------------------------------

    def mint_turn_id(self) -> str:
        self._turn_counter += 1
        return f"t-{self._turn_counter}"

    def _queued_payload(self, items: list[QueuedInput]) -> list[dict]:
        """Delivery view of queued inputs: age is computed at delivery time
        so a slow agent sees HOW stale each line is (live-test backlog)."""
        now = self._now()
        return [{**q.__dict__, "age_s": max(0, round(now - q.ts))} for q in items]

    def dispatch(
        self,
        text: str,
        turn_id: str,
        target: Session | None = None,
        origin: str = "voice",
    ) -> DispatchResult:
        s = target or self.active
        if s is None or not s.connected:
            return "no_session"
        input_bytes = utf8_size(text)
        if input_bytes > MAX_INPUT_BYTES:
            raise ValueError(
                f"input exceeds maximum size of {MAX_INPUT_BYTES} bytes"
            )
        was_idle = s.state == "idle"
        payload = {
            "status": "transcript",
            "turn_id": turn_id,
            "text": text,
            "origin": origin,
            "age_s": 0,
            # Pending review items ALWAYS ride along (§4.2) — on a live
            # voice delivery too, not just on listen.
            "queued": self._queued_payload(s.queued) + self.review_items(s.session_id),
        }
        if s.parked and self.try_deliver(s.session_id, payload):
            had_queued = bool(s.queued)
            s.queued.clear()
            if had_queued:
                self._emit("input.drained", {"session_id": s.session_id, "queued": 0})
            s.parked = False
            s.outstanding_turn_id = turn_id
            s.input_log.append(
                InputLine(ts=self._now(), text=text, origin=origin, queued=False)
            )
            self._emit_session_state(s)
            return "live"
        if len(s.queued) >= MAX_QUEUED_INPUTS:
            raise ValueError(
                f"input queue is full ({MAX_QUEUED_INPUTS} commands)"
            )
        s.queued.append(
            QueuedInput(ts=self._now(), turn_id=turn_id, text=text, origin=origin)
        )
        s.input_log.append(
            InputLine(ts=self._now(), text=text, origin=origin, queued=True)
        )
        self._emit(
            "input.queued",
            {
                "session_id": s.session_id,
                "turn_id": turn_id,
                "text": text,
                "origin": origin,
                "queued": len(s.queued),
            },
        )
        return "queued_idle" if was_idle else "queued"

    def _display_state(self, s: Session) -> str:
        """The rail dot (SPEC-WORKBENCH §6) — derived, total precedence."""
        from voco.core.agent_state import display_state

        if not s.connected:
            return "gone"
        return display_state(
            bridge_state=s.state,
            pane_hint=s.pane_hint,
            idle_for_s=max(0.0, self._now() - s.last_seen),
            handle_alive=self.handle_alive(s),
        )

    def _emit_session_state(self, s: Session) -> None:
        """Emit session.state only on actual change: a healthy listener
        re-parks every slice and must not spam identical events."""
        if s.state != s.last_state_emitted:
            s.last_state_emitted = s.state
            self._emit(
                "session.state",
                {
                    "session_id": s.session_id,
                    "state": s.state,
                    "display_state": self._display_state(s),
                },
            )

    # ---- bridge hooks --------------------------------------------------------

    def on_listen_start(self, session_id: str) -> dict | None:
        """Bridge parks a poll. Returns an immediate payload if input waits.

        Priority (SPEC-WORKBENCH §4.2): a queued transcript always wins —
        voice is the flagship. Pending review items ride along in `queued`
        so they can never be starved; with nothing else waiting they wake
        as `{status: "review"}`. Review items mint no turn_id."""
        s = self._sessions.get(session_id)
        if s is None:
            return None
        self._mark_connected(s)
        s.last_seen = self._now()
        s.outstanding_turn_id = None  # a new listen ends the working turn
        s.reviewing = False  # ...and the review turn
        review = self.review_items(session_id)
        if s.queued:
            # The CLI renders backlog entries first and the main transcript
            # last. Make the newest command the current instruction so a
            # burst A, B, C reaches the agent in chronological order with C
            # carrying the turn id (rather than the old A, C, B ordering).
            current, backlog = s.queued[-1], s.queued[:-1]
            payload = {
                "status": "transcript",
                "turn_id": current.turn_id,
                "text": current.text,
                "origin": current.origin,
                "age_s": max(0, round(self._now() - current.ts)),
                "queued": self._queued_payload(backlog) + review,
            }
            s.queued.clear()
            self._emit("input.drained", {"session_id": s.session_id, "queued": 0})
            s.outstanding_turn_id = current.turn_id
            return payload
        if review:
            s.reviewing = True
            self._emit_session_state(s)
            return {"status": "review", "items": review}
        s.parked = True
        self._emit_session_state(s)
        return None

    def wake_review(self, session_id: str) -> bool:
        """Deliver pending review items to a parked listener now (a new
        finding/ask arrived). No-op if not parked (the items ride the next
        listen — at-least-once). Returns True if it woke a parked poll."""
        s = self._sessions.get(session_id)
        if s is None or not s.parked:
            return False
        items = self.review_items(session_id)
        if not items:
            return False
        if self.try_deliver(session_id, {"status": "review", "items": items}):
            s.parked = False
            s.reviewing = True
            self._emit_session_state(s)
            return True
        return False

    def on_listen_end(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s is not None:
            s.parked = False

    def record_say(self, session_id: str, text: str, turn_id: str | None) -> bool:
        """Returns True if this session is active (say should be spoken)."""
        s = self._sessions.get(session_id)
        if s is None:
            return False
        self._mark_connected(s)
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

    def set_pane_hint(self, session_id: str, hint: str | None) -> bool:
        """Watcher observation; emits pane.hint only on change."""
        s = self._sessions.get(session_id)
        if s is None or s.pane_hint == hint:
            return False
        prev, s.pane_hint = s.pane_hint, hint
        self._emit(
            "pane.hint",
            {
                "session_id": session_id,
                "hint": hint,
                "prev": prev,
                # A hint change can flip the dot (blocked/working/gone).
                "display_state": self._display_state(s),
            },
        )
        return True

    def set_screen(
        self, session_id: str, markdown: str, title: str | None, mode: str
    ) -> None:
        s = self._sessions.get(session_id)
        if s is None:
            return
        self._mark_connected(s)
        candidate = validate_screen_candidate(s.screen_markdown, markdown, mode)
        if mode == "append":
            s.screen_markdown = candidate
        else:
            s.screen_markdown = candidate
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

    # ---- durability (dump/restore across daemon restarts) -----------------------

    STATE_VERSION = 1

    @staticmethod
    def _restore_queued(raw_items: object) -> list[QueuedInput]:
        """Keep only valid, bounded legacy queue entries, newest first by tail."""
        if not isinstance(raw_items, list):
            return []
        valid: list[QueuedInput] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                ts = float(raw["ts"])
                turn_id = raw["turn_id"]
                item_text = raw["text"]
                origin = raw.get("origin", "voice")
            except (KeyError, TypeError, ValueError):
                continue
            values = (turn_id, item_text, origin)
            if not all(isinstance(value, str) for value in values):
                continue
            if utf8_size(item_text) > MAX_INPUT_BYTES:
                continue
            valid.append(
                QueuedInput(ts=ts, turn_id=turn_id, text=item_text, origin=origin)
            )
        return valid[-MAX_QUEUED_INPUTS:]

    def dump(self) -> dict:
        """Full persistable state — tokens included (the store must hold it
        at 0600). Pure: dict out, no fs."""
        return {
            "v": self.STATE_VERSION,
            "turn_counter": self._turn_counter,
            "active_session": self._active_id,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "identity": s.identity,
                    "call_name": s.call_name,
                    "capabilities": s.capabilities,
                    "outstanding_turn_id": s.outstanding_turn_id,
                    "queued": [q.__dict__ for q in s.queued],
                    "say_log": [line.__dict__ for line in s.say_log],
                    "input_log": [line.__dict__ for line in s.input_log],
                    "unread_digest": s.unread_digest,
                    "screen_title": s.screen_title,
                    "screen_markdown": s.screen_markdown,
                    "last_seen": s.last_seen,
                }
                for s in self._sessions.values()
            ],
        }

    def restore(self, data: dict, *, max_age_s: float | None = None) -> int:
        """Rebuild from a dump; returns sessions restored. Defensive: a
        malformed entry is skipped, never fatal — losing one session beats
        refusing to boot. Restored sessions are never parked (no live poll
        survived the old daemon); outstanding turns are kept and
        self-correct on the agent's next listen."""
        if not isinstance(data, dict) or data.get("v") != self.STATE_VERSION:
            return 0
        restored = 0
        for raw in data.get("sessions", []):
            try:
                last_seen = float(raw.get("last_seen", 0.0))
                if max_age_s is not None and self._now() - last_seen > max_age_s:
                    continue
                s = Session(
                    session_id=str(raw["session_id"]),
                    identity=dict(raw["identity"]),
                    call_name=str(raw["call_name"]),
                    capabilities=list(raw["capabilities"]),
                    outstanding_turn_id=raw.get("outstanding_turn_id"),
                    queued=self._restore_queued(raw.get("queued", [])),
                    unread_digest=int(raw.get("unread_digest", 0)),
                    screen_title=raw.get("screen_title"),
                    screen_markdown=self._restore_screen(
                        raw.get("screen_markdown", "")
                    ),
                    last_seen=last_seen,
                    connected=False,
                )
                for line in raw.get("say_log", []):
                    s.say_log.append(SayLine(**line))
                for line in raw.get("input_log", []):
                    s.input_log.append(InputLine(**line))
            except (KeyError, TypeError, ValueError):
                continue
            self._sessions[s.session_id] = s
            self._by_identity[_identity_key(s.identity)] = s.session_id
            restored += 1
        counter = data.get("turn_counter", 0)
        if isinstance(counter, int) and counter > self._turn_counter:
            self._turn_counter = counter
        active = data.get("active_session")
        if active in self._sessions:
            self._active_id = active
        return restored

    @staticmethod
    def _restore_screen(raw: object) -> str:
        """Drop oversized persisted screen content rather than truncate Markdown."""
        text = str(raw or "")
        return text if utf8_size(text) <= MAX_SCREEN_BYTES else ""

    def transcript(self, session_id: str) -> dict:
        """Both halves of the conversation record (DESIGN-DECK U0),
        oldest first — the transcript tab's data source. Bounded by the
        two deques; the snapshot stays lean (say_tail only)."""
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        return {
            "session_id": s.session_id,
            "name": s.call_name,
            "inputs": [line.__dict__ for line in s.input_log],
            "says": [line.__dict__ for line in s.say_log],
        }

    # ---- snapshot (SPEC §10) ---------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "name": s.call_name,
                    "display_name": s.display_name,
                    "state": s.state,
                    "display_state": self._display_state(s),
                    "capabilities": s.capabilities,
                    "host": s.identity.get("host"),
                    "root": s.home_root,
                    "term": self.term_cells(s),
                    "unread_digest": s.unread_digest,
                    "queued": len(s.queued),
                    "pane_hint": s.pane_hint,
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
            "active_session": self.active.session_id if self.active else None,
        }
