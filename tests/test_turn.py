"""Turn machine race tests (SPEC §5) with a fake clock.

Covers the review-critical semantics: dispatch-closes-turn, pre-dispatch
merge, revision staleness, PTT skip-hold, dispatch = max(hold, route),
REOPENABLE cancel of a local reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voco.core.turn import (
    RouteDecision,
    TurnConfig,
    TurnEvents,
    TurnMachine,
    TurnState,
)


@dataclass
class Recorder:
    calls: list[tuple] = field(default_factory=list)

    def events(self) -> TurnEvents:
        return TurnEvents(
            capture_started=lambda k, r: self.calls.append(("capture_started", k, r)),
            capture_stopped=lambda k, rev: self.calls.append(("capture_stopped", k, rev)),
            chirp_requested=lambda k: self.calls.append(("chirp", k)),
            cancel_speculation=lambda k, rev: self.calls.append(("cancel", k, rev)),
            route_requested=lambda k, rev, t: self.calls.append(("route_req", k, rev, t)),
            dispatch_ready=lambda k, t, d: self.calls.append(("dispatch", k, t, d.kind)),
            local_reply_ready=lambda k, d: self.calls.append(("local_reply", k, d.kind)),
            turn_state_changed=lambda k, s: self.calls.append(("state", k, s)),
        )

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def advance(self, machine: TurnMachine, seconds: float) -> None:
        """Advance time, firing deadlines as they come due (shell contract)."""
        end = self.t + seconds
        while True:
            dl = machine.next_deadline()
            if dl is None or dl > end:
                break
            self.t = dl
            machine.on_deadline()
        self.t = end


def make(cfg: TurnConfig | None = None) -> tuple[TurnMachine, Recorder, Clock]:
    rec = Recorder()
    clock = Clock()
    m = TurnMachine(rec.events(), cfg or TurnConfig(), now=clock.now)
    return m, rec, clock


def test_happy_path_forward_dispatch_at_hold_expiry():
    m, rec, clock = make()
    m.speech_started()
    assert m.state is TurnState.CAPTURING
    clock.advance(m, 2.0)
    m.speech_ended()
    assert m.state is TurnState.HOLDING
    assert rec.names().count("capture_stopped") == 1
    assert rec.names().count("chirp") == 1

    key = m.current_key
    m.stt_final(key, 0, "run the tests")
    assert rec.of("route_req") == [("route_req", key, 0, "run the tests")]
    m.route_decided(key, 0, RouteDecision(kind="forward"))
    # Route decided before hold expiry: no dispatch yet.
    assert rec.of("dispatch") == []
    clock.advance(m, 0.9)  # past 800ms hold
    assert rec.of("dispatch") == [("dispatch", key, "run the tests", "forward")]
    assert m.state is TurnState.IDLE


def test_dispatch_waits_for_slow_route_beyond_hold():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    clock.advance(m, 1.0)  # hold expired, in ROUTING, no decision yet
    assert m.state is TurnState.ROUTING
    assert rec.of("dispatch") == []
    m.stt_final(key, 0, "hello")
    m.route_decided(key, 0, RouteDecision(kind="ack_forward", speech="on it"))
    assert rec.of("dispatch") == [("dispatch", key, "hello", "ack_forward")]


def test_pre_dispatch_speech_merges_and_stale_results_ignored():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    clock.advance(m, 0.3)
    m.speech_started()  # resume within hold: merge
    assert m.state is TurnState.CAPTURING
    assert rec.of("cancel") == [("cancel", key, 0)]
    # Stale STT final for revision 0 must be ignored.
    m.stt_final(key, 0, "stale partial thought")
    assert rec.of("route_req") == []
    m.speech_ended()
    m.stt_final(key, 1, "the whole merged thought")
    m.route_decided(key, 1, RouteDecision(kind="forward"))
    clock.advance(m, 0.9)
    assert rec.of("dispatch") == [
        ("dispatch", key, "the whole merged thought", "forward")
    ]


def test_dispatch_closes_turn_post_dispatch_speech_is_new_turn():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    m.stt_final(key, 0, "first")
    m.route_decided(key, 0, RouteDecision(kind="forward"))
    clock.advance(m, 0.9)
    assert m.state is TurnState.IDLE
    # Speech at t≈900ms — inside the old 1200ms reopen window, but the turn
    # was dispatched: this MUST be a new turn (review blocker fix).
    m.speech_started()
    new_key = m.current_key
    assert new_key != key
    # Late results for the dispatched turn are dead.
    m.stt_final(key, 0, "ghost")
    assert all(c[1] != key for c in rec.of("route_req")[1:])


def test_ptt_release_skips_hold_but_waits_for_route():
    m, rec, clock = make()
    m.ptt_pressed()
    assert m.state is TurnState.CAPTURING
    clock.advance(m, 1.5)
    m.speech_ended()  # VAD silence while PTT held: must NOT close capture
    assert m.state is TurnState.CAPTURING
    m.ptt_released()
    assert m.state is TurnState.ROUTING  # hold skipped
    key = m.current_key
    m.stt_final(key, 0, "ptt utterance")
    assert rec.of("dispatch") == []  # still needs the route decision
    m.route_decided(key, 0, RouteDecision(kind="forward"))
    assert rec.of("dispatch") == [("dispatch", key, "ptt utterance", "forward")]


def test_local_reply_stays_reopenable_then_closes():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    m.stt_final(key, 0, "what sessions are connected")
    m.route_decided(key, 0, RouteDecision(kind="answer", speech="just Helena"))
    clock.advance(m, 0.9)
    assert rec.of("local_reply") == [("local_reply", key, "answer")]
    assert m.state is TurnState.REOPENABLE
    # Resumed speech within the reopen window cancels the local reply.
    m.speech_started()
    assert rec.of("cancel") == [("cancel", key, 0)]
    assert m.state is TurnState.CAPTURING
    m.speech_ended()
    m.stt_final(key, 1, "what sessions are connected and their state")
    m.route_decided(key, 1, RouteDecision(kind="answer", speech="Helena, idle"))
    clock.advance(m, 0.9)
    assert m.state is TurnState.REOPENABLE
    clock.advance(m, 2.0)  # reopen window expires
    assert m.state is TurnState.IDLE


def test_empty_stt_final_abandons_turn():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    m.stt_final(key, 0, "   ")
    assert m.state is TurnState.IDLE
    assert rec.of("route_req") == []
    assert rec.of("dispatch") == []


def test_ptt_press_during_holding_merges():
    m, rec, clock = make()
    m.speech_started()
    m.speech_ended()
    key = m.current_key
    assert m.state is TurnState.HOLDING
    m.ptt_pressed()
    assert m.state is TurnState.CAPTURING
    assert rec.of("cancel") == [("cancel", key, 0)]
    m.ptt_released()
    assert m.state is TurnState.ROUTING
    m.stt_final(key, 1, "merged via ptt")
    m.route_decided(key, 1, RouteDecision(kind="forward"))
    assert rec.of("dispatch") == [("dispatch", key, "merged via ptt", "forward")]
